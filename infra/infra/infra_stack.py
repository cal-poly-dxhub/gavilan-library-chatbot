"""Gavilan Library Chatbot infrastructure stack.

Phase 0/1 foundation, all L1 Cfn* from aws-cdk-lib core (see docs/architecture.md).
This stack stands up the full vector store, the Bedrock Knowledge Base, and its S3
data source:

  S3 Vectors bucket + vector index (dimension 1024 = Titan v2, cosine, float32)
  KB execution role (embedding invoke + S3 source read + s3vectors data-plane)
  Bedrock Knowledge Base (VECTOR, S3 Vectors storage)
  S3 data source (type S3, FIXED_SIZE chunking) + scraper Lambda that fills it
  query-path Lambda (own role) + HTTP API (API Gateway v2), POST /query
  widget hosting: private S3 bucket + CloudFront (OAC) + BucketDeployment(widget.js)

All changeable knobs come from the repo-root config.yaml

"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import jsii
from aws_cdk import (
    AssetHashType,
    BundlingOptions,
    CfnOutput,
    Duration,
    ILocalBundling,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_bedrock as bedrock,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_s3vectors as s3vectors,
    triggers,
)
from constructs import Construct

# Repo-root app/ directory holding the Lambda handler source (app/handler.py).
# infra_stack.py is <repo>/infra/infra/infra_stack.py, so parents[2] is the repo root.
_APP_DIR = Path(__file__).resolve().parents[2] / "app"
# Repo-root frontend/ directory. Only widget.js is uploaded (mock.js / demo.html are
# dev-only and must never ship to production).
_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

# Repo-root scraper/ directory: the scraper Lambda's source (scraper.py + lambda_function.py)
# and its requirements.txt (the deps built into the Lambda layer below).
_SCRAPER_DIR = Path(__file__).resolve().parents[2] / "scraper"

# The scraper Lambda's architecture and the MATCHING manylinux wheel tag. trafilatura pulls in
# lxml and regex - compiled C extensions - so the layer must contain Linux (x86_64) wheels, not
# the macOS wheels a plain `pip install` produces on a dev Mac. Keep these two in lockstep.
_LAMBDA_PYTHON = _lambda.Runtime.PYTHON_3_13
_LAMBDA_ARCH = _lambda.Architecture.X86_64
_MANYLINUX_TAG = "manylinux2014_x86_64"
_LAMBDA_PY_TAG = "3.13"


@jsii.implements(ILocalBundling)
class _PipManylinuxLayerBundler:
    """Builds the scraper's deps as a Lambda layer using prebuilt manylinux wheels via
    `pip --platform ... --only-binary=:all:` - NO Docker, NO compiler. This is AWS's documented
    method for compiled deps (lxml/regex): --platform + --only-binary forces pip to download the
    Linux wheel matching the Lambda architecture instead of building a macOS binary that would
    fail at runtime with an ELF/Mach-O error.

    Returns False on any failure so CDK falls back to the BundlingOptions Docker `image`/`command`
    (the required fallback if a transitive dep ever lacks a manylinux wheel).
    """

    def try_bundle(self, output_dir: str, *, image=None, **_kwargs) -> bool:
        try:
            subprocess.run(
                [
                    sys.executable, "-m", "pip", "install",
                    "-r", str(_SCRAPER_DIR / "requirements.txt"),
                    "--platform", _MANYLINUX_TAG,
                    "--python-version", _LAMBDA_PY_TAG,
                    "--implementation", "cp",
                    "--only-binary=:all:",
                    "--target", str(Path(output_dir) / "python"),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            return True
        except Exception:  # no local pip / missing wheel -> let CDK try Docker bundling
            return False


class GavilanChatbotStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: Dict[str, Any],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        kb_cfg = config["knowledge_base"]
        vs_cfg = config["vector_store"]
        chunking_cfg = config["chunking"]
        scraper_cfg = config["scraper"]
        http_api_cfg = config["http_api"]
        request_cfg = config["request"]
        retrieval_cfg = config["retrieval"]
        generation_cfg = config["generation"]
        guardrail_cfg = config["guardrail"]
        catalog_cfg = config["catalog"]

        kb_name = kb_cfg["name"]
        # S3 Vectors store knobs (replaces the OpenSearch Serverless collection/index).
        vector_bucket_name = vs_cfg["vector_bucket_name"]
        index_name = vs_cfg["index_name"]
        data_type = vs_cfg["data_type"]
        distance_metric = vs_cfg["distance_metric"]
        non_filterable_keys = vs_cfg["non_filterable_metadata_keys"]

        embedding_model_arn = (
            f"arn:{self.partition}:bedrock:{self.region}"
            f"::foundation-model/{kb_cfg['embedding_model_id']}"
        )

        # --- Vector store: Amazon S3 Vectors ------------------------------------------
        #
        # S3 Vectors replaces the OpenSearch Serverless collection/index as the KB vector store:
        # near-zero cost, no cluster / VPC / FGAC / security policies. SEMANTIC-SEARCH-ONLY (no
        # hybrid) - the accepted tradeoff for the sponsor's cheap/light requirement; keyword
        # coverage is added later by agentifying the bot with a structured lookup tool (out of
        # scope here). Encryption defaults to SSE-S3 (AWS-managed keys); revisit if the sponsor
        # requires a customer-managed KMS key.
        vector_bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=vector_bucket_name,
        )

        # The vector index.
        #   - dimension MUST equal the embedding model's output (Titan Embed Text v2 = 1024) or
        #     ingestion fails; sourced from knowledge_base.vector_dimension (single source of truth).
        #   - data_type float32 is the only supported type.
        #   - distance_metric cosine: Titan v2 embeddings are normalized, so cosine ranks
        #     equivalently to the old OpenSearch l2.
        #   - non_filterable_metadata_keys is a KNOWN TRAP and is IMMUTABLE after creation:
        #     Bedrock's internal metadata keys are filterable by default and blow S3 Vectors'
        #     ~1 KB / 35-key filterable-metadata limit, failing every ingestion with
        #     ValidationException. Marking them non-filterable is the documented fix. Retrieval
        #     never filters on these keys, so it costs nothing. (Max 10 non-filterable keys; 5 used.)
        vector_index = s3vectors.CfnIndex(
            self,
            "VectorIndex",
            vector_bucket_name=vector_bucket_name,
            index_name=index_name,
            data_type=data_type,
            dimension=kb_cfg["vector_dimension"],
            distance_metric=distance_metric,
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=non_filterable_keys,
            ),
        )
        # The index lives inside the bucket, so the bucket must exist first. vector_bucket_name is
        # a literal (not a Ref), so this ordering edge is declared explicitly, not inferred.
        vector_index.add_dependency(vector_bucket)

        # --- Knowledge Base source bucket ---------------------------------------------

        # S3 bucket the KB ingests source content from. Populated out-of-band (test files for
        # now; the scraper Lambda that fills it lands in the next commit). Same private security
        # posture as the widget bucket: no public access, ACLs disabled, encrypted at rest,
        # SSL-only. One-click uninstall parity (empty + delete on `cdk destroy`).
        source_bucket = s3.Bucket(
            self,
            "KnowledgeBaseSourceBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # --- Database-catalog bucket (Phase 2b) ---------------------------------------

        # DEDICATED private bucket for the self-updating database catalog: the scraper writes the
        # regenerated held list here, the query Lambda reads it. Deliberately NOT the KB source
        # bucket - everything in that bucket gets ingested into the vector store, and the catalog
        # JSON is not KB content. Same private posture; one-click uninstall parity. The robustness
        # guard (in the scraper) keeps the last-good object rather than overwriting with garbage,
        # so plain last-write-wins storage (no versioning) is sufficient.
        catalog_bucket = s3.Bucket(
            self,
            "CatalogBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        catalog_key = catalog_cfg["s3_key"]

        # --- Knowledge Base execution role --------------------------------------------

        # Assumable by the Bedrock service. Other resources reference this object directly
        # (no ARN copy-paste).
        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            description="Execution role for the Gavilan Library Bedrock Knowledge Base.",
        )
        # Invoke the Titan embeddings model used to embed both ingested chunks and queries.
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[embedding_model_arn],
            )
        )
        # Data-plane access to the S3 Vectors index: read/write vectors + read the index. These
        # are the actions the AWS "Create a service role for Bedrock Knowledge Bases" doc lists for
        # an S3 Vectors store. Scoped to the specific index ARN (not a wildcard); the index ARN
        # already nests the bucket name (arn:...:bucket/<bucket>/index/<index>).
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3vectors:PutVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:QueryVectors",
                    "s3vectors:GetIndex",
                ],
                resources=[vector_index.attr_index_arn],
            )
        )
        # Read the S3 source content to ingest: ListBucket on the bucket, GetObject on its
        # objects. Wired through the CDK bucket object (bucket_arn / arn_for_objects), not a
        # hardcoded ARN.
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[source_bucket.bucket_arn, source_bucket.arn_for_objects("*")],
            )
        )

        # (S3 Vectors needs no data-access policy and no separate index-creator: the vector
        # bucket + index above are plain CloudFormation resources, and the KB reaches them through
        # the IAM s3vectors grant on kb_role. The OpenSearch Serverless security/network/data
        # policies and the cfn-exec-role data-access grant are gone entirely.)

        # --- Knowledge Base -----------------------------------------------------------

        knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=kb_name,
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=embedding_model_arn,
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                # S3VectorsConfiguration is a oneOf: EITHER index_arn alone, OR
                # index_name + vector_bucket_arn - never all three (all three matches BOTH
                # subschemas and CloudFormation rejects it as ambiguous at validation, before
                # anything is created). We pass index_arn alone: the index ARN already nests the
                # bucket + index name, so it fully identifies the store, and the GetAtt keeps the
                # dependency on the in-stack index (which itself depends on the bucket).
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    index_arn=vector_index.attr_index_arn,
                ),
            ),
        )
        # The KB must not be created before the index exists, and needs its role (and the role's
        # inline policy) in place. The index already depends on the vector bucket, so that ordering
        # is transitive.
        knowledge_base.add_dependency(vector_index)
        knowledge_base.node.add_dependency(kb_role)

        # --- S3 data source -----------------------------------------------------------

        # The KB ingests source content from the S3 bucket above (vector-store-agnostic, unlike
        # the managed Web Crawler which was hard-coupled to OpenSearch Serverless - that swap is
        # what unblocks moving to a cheaper vector store later). Chunking is the SAME FIXED_SIZE
        # config the crawler used, still from config.yaml (unchanged): maxTokens 300, overlap 20.
        s3_data_source = bedrock.CfnDataSource(
            self,
            "S3DataSource",
            name=f"{kb_name}-s3",
            knowledge_base_id=knowledge_base.attr_knowledge_base_id,
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=source_bucket.bucket_arn,
                ),
            ),
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy=chunking_cfg["strategy"],
                    fixed_size_chunking_configuration=bedrock.CfnDataSource.FixedSizeChunkingConfigurationProperty(
                        max_tokens=chunking_cfg["max_tokens"],
                        overlap_percentage=chunking_cfg["overlap_percentage"],
                    ),
                ),
            ),
        )
        # The data source cannot be created before the KB exists.
        s3_data_source.add_dependency(knowledge_base)

        # --- Scraper Lambda: feeds the S3 source bucket + triggers ingestion -----------

        # Dependency LAYER: trafilatura + lxml + regex + httpx as manylinux x86_64 wheels. Built
        # locally with pip --platform (no Docker); Docker bundling is the fallback if a wheel is
        # ever missing. asset_hash_type=OUTPUT so the hash tracks the built layer, not the source
        # dir (which carries .venv/tests). See _PipManylinuxLayerBundler.
        scraper_deps_layer = _lambda.LayerVersion(
            self,
            "ScraperDepsLayer",
            description="trafilatura/lxml/regex/httpx (manylinux x86_64) for the scraper Lambda.",
            compatible_runtimes=[_LAMBDA_PYTHON],
            compatible_architectures=[_LAMBDA_ARCH],
            code=_lambda.Code.from_asset(
                str(_SCRAPER_DIR),
                asset_hash_type=AssetHashType.OUTPUT,
                exclude=["*", "!requirements.txt"],
                bundling=BundlingOptions(
                    image=_LAMBDA_PYTHON.bundling_image,
                    local=_PipManylinuxLayerBundler(),
                    # Docker fallback: inside the Linux bundling image, a plain install yields
                    # correct Linux wheels natively. platform pins x86_64 even on an ARM Mac.
                    command=[
                        "bash",
                        "-c",
                        "pip install -r requirements.txt --target /asset-output/python",
                    ],
                    platform="linux/amd64",
                ),
            ),
        )

        # Its OWN execution role: basic logs + narrow S3 write + narrow StartIngestionJob.
        scraper_lambda_role = iam.Role(
            self,
            "ScraperFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Execution role for the scraper Lambda (scrape -> S3 -> start ingestion).",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        # Upload markdown + metadata sidecars into the KB source bucket (objects only, one bucket).
        scraper_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[source_bucket.arn_for_objects("*")],
            )
        )
        # Trigger ingestion of the fresh content on the specific KB (StartIngestionJob is scoped
        # to the knowledge-base ARN; it covers that KB's data sources).
        scraper_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:StartIngestionJob"],
                resources=[knowledge_base.attr_knowledge_base_arn],
            )
        )
        # Catalog bucket (Phase 2b): read the previous catalog (enrichment reuse + last-good) and
        # write the regenerated one. Scoped to the single catalog object.
        scraper_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[catalog_bucket.arn_for_objects(catalog_key)],
            )
        )
        # Enrichment model (assigns subjects/aliases). Same cross-region inference-profile grant
        # shape the query Lambda uses for generation: InvokeModel* on the profile ARN + the
        # foundation-model ARNs across routed regions, plus profile-metadata read. Falls back to a
        # single foundation-model grant for a bare (non-profile) id.
        enrichment_model_id = catalog_cfg["enrichment_model_id"]
        _enrich_head = enrichment_model_id.split(".", 1)[0]
        if "." in enrichment_model_id and _enrich_head in ("us", "eu", "apac", "us-gov"):
            _enrich_base = enrichment_model_id.split(".", 1)[1]
            scraper_lambda_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock:InvokeModel*"],
                    resources=[
                        f"arn:{self.partition}:bedrock:{self.region}:{self.account}"
                        f":inference-profile/{enrichment_model_id}",
                        f"arn:{self.partition}:bedrock:{self.region}"
                        f"::foundation-model/{_enrich_base}",
                        f"arn:{self.partition}:bedrock:*::foundation-model/{_enrich_base}",
                    ],
                )
            )
            scraper_lambda_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock:GetInferenceProfile", "bedrock:ListInferenceProfiles"],
                    resources=["*"],
                )
            )
        else:
            scraper_lambda_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock:InvokeModel"],
                    resources=[
                        f"arn:{self.partition}:bedrock:{self.region}"
                        f"::foundation-model/{enrichment_model_id}"
                    ],
                )
            )

        scraper_log_group = logs.LogGroup(
            self,
            "ScraperFunctionLogGroup",
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        scraper_lambda = _lambda.Function(
            self,
            "ScraperFunction",
            runtime=_LAMBDA_PYTHON,
            architecture=_LAMBDA_ARCH,
            handler="lambda_function.handler",
            # Ship only the two source files; deps come from the layer, boto3 from the runtime.
            code=_lambda.Code.from_asset(
                str(_SCRAPER_DIR),
                exclude=["*", "!scraper.py", "!lambda_function.py"],
            ),
            layers=[scraper_deps_layer],
            role=scraper_lambda_role,
            # ~16+ pages fetched + parsed sequentially (trafilatura is CPU-ish), then uploaded.
            timeout=Duration.minutes(5),
            memory_size=512,
            log_group=scraper_log_group,
            environment={
                "SEED_URLS": json.dumps(scraper_cfg["seed_urls"]),
                "SCRAPE_TIMEOUT_SECONDS": str(scraper_cfg.get("timeout_seconds", 20)),
                "SCRAPER_USER_AGENT": scraper_cfg.get("user_agent", ""),
                "SOURCE_BUCKET": source_bucket.bucket_name,
                "KNOWLEDGE_BASE_ID": knowledge_base.attr_knowledge_base_id,
                "DATA_SOURCE_ID": s3_data_source.attr_data_source_id,
                # Phase 2b catalog regeneration.
                "CATALOG_BUCKET": catalog_bucket.bucket_name,
                "CATALOG_KEY": catalog_key,
                "CATALOG_ENRICHMENT_MODEL_ID": enrichment_model_id,
                "CATALOG_MIN_DATABASES": str(catalog_cfg["min_databases"]),
            },
        )
        # Needs the KB + data source (it calls StartIngestionJob on them at runtime).
        scraper_lambda.node.add_dependency(s3_data_source)

        # Weekly re-scrape on a maintenance-window schedule (cron from config.yaml, UTC). Keeps
        # the KB fresh as the library site changes. EventBridge adds the invoke permission.
        events.Rule(
            self,
            "ScraperSchedule",
            description="Scheduled re-scrape of the Gavilan library site to refresh KB content.",
            schedule=events.Schedule.expression(scraper_cfg["schedule_cron"]),
            targets=[events_targets.LambdaFunction(scraper_lambda)],
        )

        # One-click install: invoke the (existing) scraper ONCE during `cdk deploy` so the KB is
        # populated the moment the stack comes up - no manual invoke, no waiting for the weekly
        # schedule. Uses the stable aws-cdk-lib `triggers.Trigger` (a CDK-managed invoker), NOT a
        # hand-rolled custom resource.
        #   - invocation_type=EVENT: fire-and-forget. The trigger succeeds once the function is
        #     invoked, REGARDLESS of the scrape's result - a flaky site, a partial-page failure, or
        #     the async ingestion never fails or blocks the deploy. (REQUEST_RESPONSE, the default,
        #     would make the deploy wait on and fail with the scraper.) The weekly schedule retries,
        #     so a one-time install hiccup is self-healing.
        #   - execute_after: run only after the scraper AND its targets exist - it writes to the
        #     source bucket and calls StartIngestionJob on the data source.
        #   - execute_on_handler_change (default True): fires on install (create) and whenever the
        #     scraper changes; a no-op redeploy does not re-fire (KB already populated; the schedule
        #     refreshes). Re-firing is harmless anyway - it overwrites the same S3 keys.
        # The Trigger grants its invoker lambda:InvokeFunction on this function automatically.
        triggers.Trigger(
            self,
            "ScraperInstallTrigger",
            handler=scraper_lambda,
            invocation_type=triggers.InvocationType.EVENT,
            execute_after=[
                scraper_lambda,
                source_bucket,
                knowledge_base,
                s3_data_source,
            ],
            execute_on_handler_change=True,
        )

        # --- Bedrock Guardrails: input screen + output backstop -----------------------

        # Two guardrails:
        #   INPUT guardrail  - applied via ApplyGuardrail(source=INPUT) on the bare query in
        #                      the handler, before retrieval. Content filters + prompt-attack
        #                      BLOCK; all PII ANONYMIZE (mask-and-proceed).
        #   OUTPUT guardrail - attached to Converse. Content filters on the answer only
        #                      (input strengths NONE so the <context> is untouched), no PII.

        # Canonical definitions of each guardrail. These drive BOTH the CfnGuardrail props
        # and the version-description hash, so the hash covers exactly what is deployed and
        # any config change forces a new published version.
        input_filters_def = [
            {
                "type": f["type"],
                "inputStrength": f["input_strength"],
                "outputStrength": f["output_strength"],
            }
            for f in guardrail_cfg["content_filters"]
        ]
        # Output guardrail: input side OFF (NONE) so the <context> in the user message is
        # never screened; PROMPT_ATTACK is input-only and is dropped entirely.
        output_filters_def = [
            {"type": f["type"], "inputStrength": "NONE", "outputStrength": f["output_strength"]}
            for f in guardrail_cfg["content_filters"]
            if f["type"] != "PROMPT_ATTACK"
        ]
        # Input screen PII: every entity ANONYMIZE (mask), so a masked query always proceeds
        # and the mask-vs-block decision is unambiguous.
        input_pii_def = [
            {"type": entity, "action": "ANONYMIZE"}
            for entity in guardrail_cfg["pii_anonymize_entities"]
        ]

        input_guardrail_def = {
            "name": guardrail_cfg["input_name"],
            "contentFilters": input_filters_def,
            "piiEntities": input_pii_def,
            "blockedInputMessaging": guardrail_cfg["blocked_input_messaging"],
            "blockedOutputsMessaging": guardrail_cfg["blocked_outputs_messaging"],
        }
        output_guardrail_def = {
            "name": guardrail_cfg["output_name"],
            "contentFilters": output_filters_def,
            "blockedInputMessaging": guardrail_cfg["blocked_input_messaging"],
            "blockedOutputsMessaging": guardrail_cfg["blocked_outputs_messaging"],
        }

        def _config_hash(payload: Dict[str, Any]) -> str:
            return hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode("utf-8")
            ).hexdigest()[:12]

        def _content_filters(defs):
            return [
                bedrock.CfnGuardrail.ContentFilterConfigProperty(
                    type=f["type"],
                    input_strength=f["inputStrength"],
                    output_strength=f["outputStrength"],
                )
                for f in defs
            ]

        input_guardrail = bedrock.CfnGuardrail(
            self,
            "InputGuardrail",
            name=input_guardrail_def["name"],
            description=(
                "Input screen for the Gavilan Library chatbot, applied via "
                "ApplyGuardrail(source=INPUT) on the bare user query before retrieval: "
                "content filters + prompt-attack BLOCK, all PII ANONYMIZE."
            ),
            blocked_input_messaging=input_guardrail_def["blockedInputMessaging"],
            blocked_outputs_messaging=input_guardrail_def["blockedOutputsMessaging"],
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=_content_filters(input_guardrail_def["contentFilters"]),
            ),
            sensitive_information_policy_config=(
                bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                    pii_entities_config=[
                        bedrock.CfnGuardrail.PiiEntityConfigProperty(
                            type=e["type"], action=e["action"]
                        )
                        for e in input_guardrail_def["piiEntities"]
                    ],
                )
            ),
        )

        # Output backstop: content filters ONLY, output side. No sensitive-information policy
        # (retrieved library info is public; masking the answer would re-break contact answers).
        output_guardrail = bedrock.CfnGuardrail(
            self,
            "OutputGuardrail",
            name=output_guardrail_def["name"],
            description=(
                "Output backstop for the Gavilan Library chatbot, attached to Converse: "
                "content filters on the generated answer only (input strengths NONE, no PII)."
            ),
            blocked_input_messaging=output_guardrail_def["blockedInputMessaging"],
            blocked_outputs_messaging=output_guardrail_def["blockedOutputsMessaging"],
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=_content_filters(output_guardrail_def["contentFilters"]),
            ),
        )

        # Numbered, immutable versions the Lambda pins to. The description carries a content
        # hash of the resolved guardrail config: CfnGuardrailVersion has no other property
        # that changes when config.yaml changes, so without this a guardrail edit updates the
        # DRAFT but never publishes a new version and the Lambda stays on the stale one.
        # Hashing is used rather than pinning to DRAFT, which is mutable with no immutability,
        # rollback, or reproducibility.
        input_guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "InputGuardrailVersion",
            guardrail_identifier=input_guardrail.attr_guardrail_id,
            description=f"input config-{_config_hash(input_guardrail_def)}",
        )
        output_guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "OutputGuardrailVersion",
            guardrail_identifier=output_guardrail.attr_guardrail_id,
            description=f"output config-{_config_hash(output_guardrail_def)}",
        )

        # --- Query path: Lambda + HTTP API --------------------------------------------

        generation_model_id = generation_cfg["model_id"]

        # The Lambda gets its OWN execution role, DISTINCT from the KB execution role.
        # Basic execution (CloudWatch Logs) via the managed policy; Bedrock permissions
        # are added narrowly below.
        query_lambda_role = iam.Role(
            self,
            "QueryLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Execution role for the query-path Lambda (retrieve + generate).",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        # Retrieve chunks from the KB.
        query_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:Retrieve"],
                resources=[knowledge_base.attr_knowledge_base_arn],
            )
        )
        # Invoke the generation model (Converse maps to bedrock:InvokeModel*). Modern Claude
        # models are invoked through a CROSS-REGION INFERENCE PROFILE (a geographic-prefixed
        # id like "us.anthropic..."), which needs a different IAM shape than a bare on-demand
        # foundation-model id. Branch on the id form so a bare id still works:
        #   - profile: InvokeModel* on the account+region-scoped inference-profile ARN PLUS
        #     the foundation-model ARNs in the source region (this stack's region) and every
        #     destination region the profile routes to; then read access to profile metadata.
        #   - bare id: InvokeModel on the single region-scoped foundation-model ARN.
        # Account/region come from Stack tokens (self.account/self.region), so nothing is
        # hardcoded and one-click deploy stays region-agnostic.
        _GEO_PREFIXES = ("us", "eu", "apac", "us-gov")
        _head = generation_model_id.split(".", 1)[0]
        is_inference_profile = "." in generation_model_id and _head in _GEO_PREFIXES

        if is_inference_profile:
            # Underlying foundation-model id = profile id minus the geographic prefix.
            base_model_id = generation_model_id.split(".", 1)[1]
            inference_profile_arn = (
                f"arn:{self.partition}:bedrock:{self.region}:{self.account}"
                f":inference-profile/{generation_model_id}"
            )
            # Source region = this stack's region. Destination regions the profile routes to
            # cannot be enumerated without hardcoding, so use a region wildcard on the same
            # (single) model id - the AWS-recommended grant for cross-region inference.
            source_region_model_arn = (
                f"arn:{self.partition}:bedrock:{self.region}::foundation-model/{base_model_id}"
            )
            routed_region_model_arn = (
                f"arn:{self.partition}:bedrock:*::foundation-model/{base_model_id}"
            )
            query_lambda_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock:InvokeModel*"],
                    resources=[
                        inference_profile_arn,
                        source_region_model_arn,
                        routed_region_model_arn,
                    ],
                )
            )
            # Resolve the profile's metadata/routing at runtime. ListInferenceProfiles has no
            # resource-level scoping (must be "*"); GetInferenceProfile is read-only metadata.
            query_lambda_role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "bedrock:GetInferenceProfile",
                        "bedrock:ListInferenceProfiles",
                    ],
                    resources=["*"],
                )
            )
        else:
            generation_model_arn = (
                f"arn:{self.partition}:bedrock:{self.region}"
                f"::foundation-model/{generation_model_id}"
            )
            query_lambda_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock:InvokeModel"],
                    resources=[generation_model_arn],
                )
            )
        # ApplyGuardrail on BOTH guardrails: the standalone input screen (source=INPUT) and
        # the guardrail attached to the Converse call both require this action on their ARN.
        query_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[
                    input_guardrail.attr_guardrail_arn,
                    output_guardrail.attr_guardrail_arn,
                ],
            )
        )
        # Read the self-updating database catalog the scraper writes (Phase 2b). Read-only, scoped
        # to the single catalog object.
        query_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[catalog_bucket.arn_for_objects(catalog_key)],
            )
        )

        # Explicit log group so retention is bounded and it is torn down with the stack,
        # rather than the implicit never-expiring group Lambda would create on first invoke
        # and leave orphaned on destroy.
        query_log_group = logs.LogGroup(
            self,
            "QueryFunctionLogGroup",
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        query_lambda = _lambda.Function(
            self,
            "QueryFunction",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="handler.lambda_handler",
            # Ship the handler, the system prompt, and the static database catalog (under data/);
            # keep __pycache__ / stray files out so the asset hash tracks real source changes.
            # Re-including a nested file needs its parent dir un-excluded too, hence "!data".
            code=_lambda.Code.from_asset(
                str(_APP_DIR),
                exclude=[
                    "*",
                    "!handler.py",
                    "!system_prompt.md",
                    "!data",
                    "!data/database_catalog.json",
                ],
            ),
            role=query_lambda_role,
            timeout=Duration.seconds(30),
            memory_size=256,
            log_group=query_log_group,
            environment={
                "KNOWLEDGE_BASE_ID": knowledge_base.attr_knowledge_base_id,
                "GENERATION_MODEL_ID": generation_model_id,
                "NUMBER_OF_RESULTS": str(retrieval_cfg["number_of_results"]),
                "BEDROCK_REGION": self.region,
                # Generation inference knobs + server-side query length cap, wired from
                # config.yaml so edits reach runtime.
                "GENERATION_MAX_TOKENS": str(generation_cfg["max_tokens"]),
                "GENERATION_TEMPERATURE": str(generation_cfg["temperature"]),
                "MAX_QUERY_CHARS": str(request_cfg["max_query_chars"]),
                # Input screen (ApplyGuardrail source=INPUT, pre-retrieval) + output backstop
                # (attached to Converse). Each pins to its own published numbered version.
                "INPUT_GUARDRAIL_ID": input_guardrail.attr_guardrail_id,
                "INPUT_GUARDRAIL_VERSION": input_guardrail_version.attr_version,
                "OUTPUT_GUARDRAIL_ID": output_guardrail.attr_guardrail_id,
                "OUTPUT_GUARDRAIL_VERSION": output_guardrail_version.attr_version,
                "GUARDRAIL_TRACE": guardrail_cfg.get("trace", "enabled"),
                # Phase 2b: read the self-updating held catalog from S3 (bundled JSON is the seed +
                # not-held source + fallback). Cache TTL bounds per-container staleness.
                "CATALOG_BUCKET": catalog_bucket.bucket_name,
                "CATALOG_KEY": catalog_key,
                "CATALOG_CACHE_TTL_SECONDS": str(catalog_cfg["cache_ttl_seconds"]),
            },
        )
        # The Lambda queries the KB at runtime, so it must not exist before the KB.
        query_lambda.node.add_dependency(knowledge_base)

        # HTTP API (API Gateway v2), NOT REST: ~71% cheaper for a Lambda-proxy job and we
        # need none of the REST-only features. CORS is permissive for now.
        # TODO: lock allow_origins to the library widget domain before launch.
        # GET is allowed for the widget's fire-and-forget GET /warm ping.
        http_api = apigwv2.HttpApi(
            self,
            "ChatbotHttpApi",
            api_name="gavilan-library-chatbot",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["Content-Type"],
            ),
        )
        # One Lambda integration, reused for both routes. HttpLambdaIntegration defaults to
        # payload format version 2.0.
        query_integration = apigwv2_integrations.HttpLambdaIntegration(
            "QueryIntegration", query_lambda
        )
        http_api.add_routes(
            path="/query",
            methods=[apigwv2.HttpMethod.POST],
            integration=query_integration,
        )
        # Lightweight pre-warm route: the widget pings GET /warm on load to warm the query
        # Lambda (retrieve-only) before the student's first query. (S3 Vectors itself has no
        # cluster to wake; this just avoids a cold Lambda on the first real query.)
        http_api.add_routes(
            path="/warm",
            methods=[apigwv2.HttpMethod.GET],
            integration=query_integration,
        )
        # Stage-level throttling on the default stage: the load-bearing cost-abuse control for
        # this public, unauthenticated endpoint (it is why WAF was excluded). Applying it to the
        # default route settings covers every route (/query and /warm); over the limit, API
        # Gateway returns HTTP 429. Values come from config.
        default_stage = http_api.default_stage.node.default_child
        default_stage.default_route_settings = apigwv2.CfnStage.RouteSettingsProperty(
            throttling_rate_limit=http_api_cfg["throttling_rate_limit"],
            throttling_burst_limit=http_api_cfg["throttling_burst_limit"],
        )

        # --- Widget hosting: private S3 bucket + CloudFront (OAC) ----------------------

        # The production widget file is served from a private S3 bucket, reachable ONLY
        # through CloudFront via Origin Access Control (OAC). It lives in THIS stack (not a
        # separate one) so the whole thing installs with one `cdk deploy`: OAC has a known
        # cross-stack cyclical-dependency problem, and one-click install wants a single
        # deploy.
        widget_bucket = s3.Bucket(
            self,
            "WidgetBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            # OAC requires ACLs disabled (bucket-owner-enforced ownership). That is the
            # default for freshly created buckets; we set it explicitly to keep the OAC
            # requirement legible.
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # One-click install implies one-click uninstall: on `cdk destroy`, remove the
            # bucket and empty it first (auto_delete_objects adds a small custom resource).
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # OAC origin via the CURRENT L2 construct (NOT the deprecated S3Origin/OAI, NOT a
        # hand-rolled CfnOriginAccessControl). with_origin_access_control provisions the
        # OAC and wires the bucket policy so only this distribution can read the object.
        widget_origin = origins.S3BucketOrigin.with_origin_access_control(widget_bucket)

        widget_distribution = cloudfront.Distribution(
            self,
            "WidgetDistribution",
            comment="Gavilan Library chat widget CDN (serves widget.js).",
            default_root_object="widget.js",
            default_behavior=cloudfront.BehaviorOptions(
                origin=widget_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
        )
        # NOTE: do NOT add an explicit distribution->bucket dependency. The origin already
        # references the bucket (GetAtt), so CloudFormation orders the bucket first. Adding
        # node.add_dependency(bucket) instead pulls in ALL the bucket's children, including
        # the auto-delete-objects custom resource, which DependsOn the OAC bucket policy,
        # which DependsOn the distribution -> a synth-blocking dependency cycle.

        # Upload ONLY frontend/widget.js into the bucket. The include-only exclude pattern
        # ("*" then "!widget.js") keeps mock.js, demo.html, and any future dev files out of
        # production. On deploy, invalidate the CloudFront cache for the file so an updated
        # widget.js is served immediately instead of from edge cache.
        s3deploy.BucketDeployment(
            self,
            "WidgetDeployment",
            destination_bucket=widget_bucket,
            sources=[
                s3deploy.Source.asset(
                    str(_FRONTEND_DIR), exclude=["*", "!widget.js"]
                )
            ],
            distribution=widget_distribution,
            distribution_paths=["/widget.js"],
        )

        # --- Outputs: ready-to-paste embed tag + raw domain / API URL ------------------

        # http_api.api_endpoint has NO trailing slash; the POST route is /query. The
        # CloudFront domain has no scheme, so widget.js is served at https://<domain>/widget.js.
        widget_src = f"https://{widget_distribution.distribution_domain_name}/widget.js"
        query_url = f"{http_api.api_endpoint}/query"
        embed_tag = (
            f'<script src="{widget_src}" data-api-url="{query_url}" defer></script>'
        )

        CfnOutput(
            self,
            "WidgetEmbedTag",
            value=embed_tag,
            description="Ready-to-paste embed snippet for the library website.",
        )
        CfnOutput(
            self,
            "WidgetCdnDomain",
            value=widget_distribution.distribution_domain_name,
            description="CloudFront domain name serving widget.js.",
        )
        CfnOutput(
            self,
            "ChatbotApiUrl",
            value=query_url,
            description="HTTP API POST /query endpoint the widget calls.",
        )
        CfnOutput(
            self,
            "KnowledgeBaseId",
            value=knowledge_base.attr_knowledge_base_id,
            description="Bedrock Knowledge Base id (for eval/eval_config.yaml).",
        )
