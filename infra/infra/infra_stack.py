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
  demo site: its OWN private bucket + CloudFront (OAC) serving one deploy-stamped page

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
    aws_apigatewayv2_authorizers as apigwv2_authorizers,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_bedrock as bedrock,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_s3vectors as s3vectors,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    triggers,
)
from constructs import Construct

from infra.config import (
    resolve_cors_allow_origins,
    resolve_feedback,
    resolve_scraper_tiers,
    resolve_seed_urls,
)

# Repo-root app/ directory holding the Lambda handler source (app/handler.py).
# infra_stack.py is <repo>/infra/infra/infra_stack.py, so parents[2] is the repo root.
_APP_DIR = Path(__file__).resolve().parents[2] / "app"
# Repo-root frontend/ directory. The widget bucket takes ONLY widget.js (mock.js /
# demo.html / demo-live.html are dev-only and must never ship). The demo site takes ONLY
# demo-site.html, into a separate bucket of its own - see the Demo site sections below.
_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

# The shipped demo page and the two placeholders the deploy stamps into it. Keeping the
# names here (rather than inline) makes the synth-time assertion below the single place
# that couples this stack to the page's markup.
_DEMO_PAGE_FILE = "demo-site.html"
_DEMO_API_URL_TOKEN = "__API_URL__"
_DEMO_WIDGET_SRC_TOKEN = "__WIDGET_SRC__"
# The demo page's embed also carries the sign-in gate's Cognito ids (see the auth section).
# Same mechanism as the two URLs above: placeholders here, CDK tokens resolved at deploy.
_DEMO_USER_POOL_ID_TOKEN = "__USER_POOL_ID__"
_DEMO_CLIENT_ID_TOKEN = "__CLIENT_ID__"
# The demo page's cost meter/estimator reads its rates and measured constants from
# config.yaml's cost_model block, stamped in here as a JSON literal. Unlike the two URLs
# above this is a SYNTH-time value (no CDK token), but it goes through the same placeholder
# mechanism so there is one way the page gets its deploy-time data, not two.
_DEMO_COST_MODEL_TOKEN = "__COST_MODEL__"

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
        # Scraper freshness tiers: {tier: {"schedule_cron": ..., "urls": [...]}}. Validated in
        # infra/config.py (every tier has a cron and URLs, no URL in two tiers), so a malformed
        # tier block fails at synth instead of deploying a schedule that scrapes nothing.
        scraper_tiers = resolve_scraper_tiers(config)
        # Every configured URL across all tiers - what a full run fetches and what the prune keeps.
        seed_urls = resolve_seed_urls(config)
        # A KB-excluded URL that is not actually seeded is a silent no-op that reads like a
        # working exclusion, and for databases.php it would freeze the database catalog at its
        # last-good copy with nothing in the logs to say so. Cheap to catch at synth.
        _orphan_exclusions = [
            url for url in (scraper_cfg.get("kb_exclude_urls") or []) if url not in seed_urls
        ]
        if _orphan_exclusions:
            raise ValueError(
                f"scraper.kb_exclude_urls names URLs that no tier scrapes: {_orphan_exclusions}. "
                "An exclusion only means 'fetch this but do not index it', so the URL has to be "
                "listed under one of the scraper.tiers as well."
            )
        http_api_cfg = config["http_api"]
        # Browser origin allowlist for the HTTP API. Resolved (and wildcard-rejected) in
        # infra/config.py so the "never *" rule is enforced at synth, not just by convention.
        cors_allow_origins = resolve_cors_allow_origins(config)
        request_cfg = config["request"]
        retrieval_cfg = config["retrieval"]
        generation_cfg = config["generation"]
        guardrail_cfg = config["guardrail"]
        catalog_cfg = config["catalog"]
        # Live Primo book/media catalog tool (search_book_catalog) behavioral knobs. Optional so a
        # config without a `primo` block still synths; the handler carries matching defaults.
        primo_cfg = config.get("primo", {})
        # Curated library_links tool: a STATIC table bundled with the query Lambda (no S3, no
        # scraper, no TTL), so its one knob is the bundled filename. Read once here and used for
        # BOTH the asset include and the handler env var, so a rename cannot leave the Lambda
        # bundling one file and reading another. Optional; the handler carries the same default.
        library_links_file = config.get("library_links", {}).get("data_file", "library_links.json")
        # Shareable demo site. On by default so `cdk deploy` always hands back something the
        # client can open; a launched install can turn it off in config.yaml and the next deploy
        # removes the bucket, the distribution, and its CORS origin with it. Optional block so a
        # config predating this feature still synths.
        demo_site_enabled = bool(config.get("demo_site", {}).get("enabled", True))
        # Feedback endpoint (POST /feedback -> SNS email). Resolved in infra/config.py, which
        # decides between "off", "on with a destination", and "misconfigured" - a malformed
        # address fails synth there rather than deploying a subscription nobody can confirm.
        feedback_cfg = resolve_feedback(config)

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
        # hybrid) - the accepted cost/simplicity tradeoff; keyword coverage is added later by
        # agentifying the bot with a structured lookup tool (out of scope here). Encryption
        # defaults to SSE-S3 (AWS-managed keys); revisit if a customer-managed KMS key is required.
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
        # what unblocks moving to a cheaper vector store later). Chunking comes from config.yaml.
        #
        # THE NAME CARRIES THE CHUNKING CONFIG ON PURPOSE. Chunking is immutable in Bedrock, so
        # any change to it makes CloudFormation REPLACE this resource - and CloudFormation
        # replaces by creating the new resource before deleting the old one. With a fixed name
        # that collides inside the knowledge base and the deploy dies mid-update:
        #   "DataSource with name gavilan-library-kb-s3 already exists (409 AlreadyExists)"
        # Folding the chunking settings into the name makes the replacement name unique, so a
        # chunking change is a config.yaml edit plus `cdk deploy` instead of manual AWS surgery.
        # dataDeletionPolicy is DELETE (the Bedrock default), so the old chunks leave the vector
        # index with the old data source rather than lingering alongside the new ones.
        #
        # The replacement starts EMPTY: deleting the old data source drops its vectors, and the
        # new one has ingested nothing. The source bucket is untouched, so re-ingestion just needs
        # an ingestion job - see the post-deploy note in CLAUDE.md.
        chunk_suffix = "-".join(
            [
                chunking_cfg["strategy"].lower().replace("_", ""),
                f"{chunking_cfg['max_tokens']}t{chunking_cfg['overlap_percentage']}p",
            ]
        )
        s3_data_source = bedrock.CfnDataSource(
            self,
            "S3DataSource",
            name=f"{kb_name}-s3-{chunk_suffix}",
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
        # Upload markdown + metadata sidecars into the KB source bucket, and DELETE the ones the
        # seed list no longer calls for. Without the delete the uploader could only ever add: a page
        # removed from seed_urls kept its document in the bucket and stayed indexed forever, so
        # de-seeding was a silent no-op. See lambda_function.prune_stale_objects.
        scraper_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:DeleteObject"],
                resources=[source_bucket.arn_for_objects("*")],
            )
        )
        # READ the source objects, which is what change gating runs on: the scraper HEADs each
        # markdown object to read back the `content-sha256` it stamped there last time, and
        # uploads only when the fresh content hashes differently. HeadObject is authorized as
        # s3:GetObject, so this grant is what makes an unchanged page cost nothing.
        scraper_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[source_bucket.arn_for_objects("*")],
            )
        )
        # ListBucket is granted on the BUCKET arn, not the object arn - the prune has to enumerate
        # what is actually there before it can tell what is stale, and the ingestion decision reads
        # the same listing's LastModified times.
        scraper_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[source_bucket.bucket_arn],
            )
        )
        # Trigger ingestion of the fresh content on the specific KB (StartIngestionJob is scoped
        # to the knowledge-base ARN; it covers that KB's data sources). ListIngestionJobs comes
        # with it because the scraper checks the job history before starting: Bedrock allows one
        # job per data source at a time, so an overlap between the two schedules has to be
        # detected and skipped, and the last job's start time is how a skipped change is found
        # again on the next run without storing anything.
        scraper_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:StartIngestionJob", "bedrock:ListIngestionJobs"],
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
                # The WHOLE tier map, not just one tier's URLs: each EventBridge rule below names
                # a tier in its event payload, and the Lambda looks it up here. Handing over the
                # full map also lets the stale-object prune key off every configured URL, which is
                # what stops a three-page fast run from deleting the rest of the corpus.
                "SCRAPER_TIERS": json.dumps(scraper_tiers),
                # Seed URLs fetched for their side effects but kept OUT of the knowledge base
                # (databases.php: regenerate_catalog needs its HTML, the KB does not need its text).
                "KB_EXCLUDE_URLS": json.dumps(scraper_cfg.get("kb_exclude_urls", [])),
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

        # ONE SCHEDULE PER FRESHNESS TIER, built by iterating the tier map - so adding a tier,
        # retiming one, or moving a page between them is a config.yaml edit and nothing else.
        # Each rule passes its tier name as the event payload; the Lambda resolves that to the
        # URLs it should fetch. EventBridge adds the invoke permission per rule.
        #
        # The tiers exist to answer "we changed our hours, when does the bot know?": the fast tier
        # carries the pages that hold hours, closures and dated announcements and runs daily, while
        # everything else rides the slower full sweep. Scraping more often is affordable because
        # the Lambda gates on content hashes - an unchanged page uploads nothing, starts no
        # ingestion job, and never reaches the catalog enrichment model call.
        for tier_name, tier in scraper_tiers.items():
            events.Rule(
                self,
                f"ScraperSchedule{tier_name.capitalize()}",
                description=(
                    f"Scheduled '{tier_name}' re-scrape of the Gavilan library site "
                    f"({len(tier['urls'])} URL(s) declared) to refresh KB content."
                ),
                schedule=events.Schedule.expression(tier["schedule_cron"]),
                targets=[
                    events_targets.LambdaFunction(
                        scraper_lambda,
                        event=events.RuleTargetInput.from_object({"tier": tier_name}),
                    )
                ],
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
        #
        # WHAT "WHENEVER THE SCRAPER CHANGES" ACTUALLY MEANS, since it is easy to assume every
        # deploy re-scrapes and it does not. execute_on_handler_change ties this trigger to the
        # function's currentVersion, so the synthesized HandlerArn is a Ref to a
        # `ScraperFunctionCurrentVersion<hash>` resource whose LOGICAL ID hashes the function's
        # code asset plus its configuration (env vars, layers, memory, timeout, role). Deploy
        # something that leaves all of those alone - a widget tweak, a demo-page edit, a query
        # Lambda change - and the logical id is identical, this custom resource's properties are
        # unchanged, CloudFormation does not re-run it, and NO SCRAPE HAPPENS. That is why recent
        # deploys did not appear to fire one. Deliberately left as-is.
        #
        # The invocation carries no tier, which scraper._requested_tier maps to the complete
        # sweep - the right behaviour for an install, and unchanged by the tiering work.
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

        # --- Bedrock Guardrail: the input screen, and nothing else --------------------
        #
        # ONE guardrail, screening PROMPT_ATTACK on the input side. It is applied via
        # ApplyGuardrail(source=INPUT) on the bare query in the handler, before the loop;
        # nothing is attached to Converse, so there is no output guardrail here to find.
        #
        # Everything else came out deliberately (see the guardrail block in config.yaml):
        # the screen runs ahead of the system prompt, so a content-filter block or a silent
        # PII rewrite pre-empts the prompt's crisis handling. The prompt owns safety;
        # PROMPT_ATTACK stays because it is an attack on the prompt itself.

        # Canonical definition of the guardrail. It drives BOTH the CfnGuardrail props and
        # the version-description hash, so the hash covers exactly what is deployed and any
        # config change forces a new published version.
        #
        # Output strength is NONE on every filter and is NOT a config knob: this guardrail is
        # only ever applied to input, and AWS requires NONE for PROMPT_ATTACK regardless.
        filters_def = [
            {
                "type": f["type"],
                "inputStrength": f["input_strength"],
                "outputStrength": "NONE",
            }
            for f in guardrail_cfg["content_filters"]
        ]

        guardrail_def = {
            "name": guardrail_cfg["name"],
            "contentFilters": filters_def,
            "blockedInputMessaging": guardrail_cfg["blocked_input_messaging"],
        }

        def _config_hash(payload: Dict[str, Any]) -> str:
            return hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode("utf-8")
            ).hexdigest()[:12]

        input_guardrail = bedrock.CfnGuardrail(
            self,
            "InputGuardrail",
            name=guardrail_def["name"],
            # CloudFormation caps this at 200 characters and rejects the change set at deploy
            # time if it is longer (the L1 does not validate it at synth). The reasoning that
            # does not fit lives in the comment block above, not here.
            description=(
                "Input screen for the Gavilan Library chatbot: ApplyGuardrail(source=INPUT) "
                "on the bare user query, PROMPT_ATTACK only. No other content filter and no "
                "PII policy - the system prompt owns safety."
            ),
            blocked_input_messaging=guardrail_def["blockedInputMessaging"],
            # CloudFormation requires a blocked-outputs message on every guardrail. This one
            # is unreachable by construction - the guardrail is never applied to output - so
            # it reuses the input message rather than carrying a second string to maintain.
            blocked_outputs_messaging=guardrail_def["blockedInputMessaging"],
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=f["type"],
                        input_strength=f["inputStrength"],
                        output_strength=f["outputStrength"],
                    )
                    for f in guardrail_def["contentFilters"]
                ],
            ),
            # NO sensitive_information_policy_config: PII anonymization would rewrite the
            # student's message before the model ever reads it.
        )

        # Numbered, immutable version the Lambda pins to. The description carries a content
        # hash of the resolved guardrail config: CfnGuardrailVersion has no other property
        # that changes when config.yaml changes, so without this a guardrail edit updates the
        # DRAFT but never publishes a new version and the Lambda stays on the stale one.
        # Hashing is used rather than pinning to DRAFT, which is mutable with no immutability,
        # rollback, or reproducibility.
        input_guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "InputGuardrailVersion",
            guardrail_identifier=input_guardrail.attr_guardrail_id,
            description=f"input config-{_config_hash(guardrail_def)}",
        )

        # --- Demo site (1/2): its own private bucket + CloudFront ----------------------
        #
        # A public, shareable page that shows the widget sitting in a Gavilan-Library-looking
        # site, so the deployment itself is the demo: one `cdk deploy` produces a link the
        # client can open with no setup. The page is built here in TWO parts because the API's
        # CORS allowlist has to name this distribution's domain, so the distribution must exist
        # before the HTTP API; the page it serves needs the API URL, so its content is uploaded
        # after both (part 2/2, below the widget hosting section).
        #
        # DELIBERATELY ITS OWN BUCKET AND ITS OWN DISTRIBUTION, not a second prefix on the
        # widget's:
        #   - BucketDeployment prunes by default (`aws s3 sync --delete`), so two deployments
        #     into one bucket fight - whichever runs last deletes the other's objects unless
        #     both are fenced with prefixes/excludes. Separate buckets make that impossible
        #     rather than merely configured-correctly.
        #   - Production widget delivery is the thing that must not regress. Nothing here
        #     touches the widget bucket, its deployment, or its distribution, so the embed URL
        #     the library pastes on their site is unaffected by anything the demo does.
        #   - A separate distribution can carry demo-only response headers (noindex) without
        #     putting them on the production widget.
        # Cost is one extra CloudFront distribution and one small bucket.
        demo_bucket = None
        demo_distribution = None
        demo_origins: list = []
        if demo_site_enabled:
            demo_bucket = s3.Bucket(
                self,
                "DemoSiteBucket",
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
                encryption=s3.BucketEncryption.S3_MANAGED,
                enforce_ssl=True,
                removal_policy=RemovalPolicy.DESTROY,
                auto_delete_objects=True,
            )

            # noindex at the EDGE, alongside the page's own <meta name="robots">. A crawler that
            # never parses the HTML still gets the header, and the header cannot be lost by an
            # edit to the page. Scoped to this distribution, so the production widget is untouched.
            demo_headers = cloudfront.ResponseHeadersPolicy(
                self,
                "DemoSiteHeadersPolicy",
                comment="Keeps the Gavilan chatbot demo page out of search indexes.",
                custom_headers_behavior=cloudfront.ResponseCustomHeadersBehavior(
                    custom_headers=[
                        cloudfront.ResponseCustomHeader(
                            header="X-Robots-Tag", value="noindex, nofollow", override=True
                        )
                    ]
                ),
            )

            demo_distribution = cloudfront.Distribution(
                self,
                "DemoSiteDistribution",
                comment="Gavilan Library chatbot DEMO site (not the real library site).",
                # The shareable link is the bare domain; CloudFront maps / to this object.
                default_root_object="index.html",
                default_behavior=cloudfront.BehaviorOptions(
                    origin=origins.S3BucketOrigin.with_origin_access_control(demo_bucket),
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                    response_headers_policy=demo_headers,
                ),
            )
            # The browser Origin for this page. The demo runs the REAL embed - a cross-origin
            # POST from the page to the HTTP API - so it is subject to the same CORS allowlist
            # as the library's site, and has to be listed like any other real origin. Resolved
            # at deploy from the distribution (never hardcoded), so a fresh install in another
            # account allowlists its own demo domain automatically.
            demo_origins = [f"https://{demo_distribution.distribution_domain_name}"]

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
        # ApplyGuardrail on the ONE guardrail: the standalone input screen (source=INPUT).
        # Nothing is attached to Converse, so there is no second ARN to grant.
        query_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[input_guardrail.attr_guardrail_arn],
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
            # Ship the handler, the system prompt, and the static data files under data/ (the
            # database-catalog seed + the curated library_links table); keep __pycache__ / stray
            # files out so the asset hash tracks real source changes. Re-including a nested file
            # needs its parent dir un-excluded too, hence "!data".
            code=_lambda.Code.from_asset(
                str(_APP_DIR),
                exclude=[
                    "*",
                    "!handler.py",
                    "!system_prompt.md",
                    "!data",
                    "!data/database_catalog.json",
                    f"!data/{library_links_file}",
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
                # The input screen (ApplyGuardrail source=INPUT, pre-loop), pinned to its
                # published numbered version. There is no OUTPUT_GUARDRAIL_* pair and no
                # GUARDRAIL_TRACE: nothing is attached to Converse, so there is no trace
                # to configure.
                "INPUT_GUARDRAIL_ID": input_guardrail.attr_guardrail_id,
                "INPUT_GUARDRAIL_VERSION": input_guardrail_version.attr_version,
                # Phase 2b: read the self-updating held catalog from S3 (bundled JSON is the seed +
                # not-held source + fallback). Cache TTL bounds per-container staleness.
                "CATALOG_BUCKET": catalog_bucket.bucket_name,
                "CATALOG_KEY": catalog_key,
                "CATALOG_CACHE_TTL_SECONDS": str(catalog_cfg["cache_ttl_seconds"]),
                # Curated library_links table, bundled above from the same config value. No IAM
                # and no bucket: the handler just reads this file out of its own asset at import.
                "LIBRARY_LINKS_FILE": library_links_file,
                # Live Primo book/media catalog tool (search_book_catalog). No IAM: it is an
                # outbound HTTPS call, not an AWS API. Knobs from config.yaml (handler has defaults).
                "PRIMO_TIMEOUT_SECONDS": str(primo_cfg.get("timeout_seconds", 5)),
                "PRIMO_NUMBER_OF_RESULTS": str(primo_cfg.get("number_of_results", 4)),
                "PRIMO_AVAILABILITY_BUDGET_SECONDS": str(
                    primo_cfg.get("availability_budget_seconds", 8)
                ),
            },
        )
        # The Lambda queries the KB at runtime, so it must not exist before the KB.
        query_lambda.node.add_dependency(knowledge_base)

        # --- Sign-in gate for /query: Cognito user pool + app client ------------------
        #
        # TEMPORARY, AND IT COMES OFF AT GO-LIVE. The bot is not on gavilan.edu yet, so the only
        # thing driving /query is a demo link that anyone with the URL can point at billable
        # Bedrock spend. Throttling caps the rate, not the bill, and CORS is browser-enforced
        # only - neither is an answer to "a stranger found the link".
        #
        # Deliberately NOT behind a config flag. A gate you can turn off from a YAML file is a
        # gate that gets left off, and there is no environment here to fork: one stack, one demo
        # URL, one shared account. Removing it at go-live is a code deletion, which is visible in
        # review, rather than a value change nobody sees.
        #
        # The pool holds ONE shared account, created by hand after deploy (see the
        # DemoAuthSetupCommands output). No credentials in the repo, none in CDK, none in
        # CloudFormation parameters - a password in a template is a password in the console, in
        # the changeset, and in the stack events.
        demo_auth_pool = cognito.UserPool(
            self,
            "DemoAuthUserPool",
            user_pool_name="gavilan-library-demo-access",
            # Nobody signs themselves up: the captain creates the one account with the CLI.
            self_sign_up_enabled=False,
            # Email-as-username (SignInAliases without `username` synthesizes UsernameAttributes,
            # not AliasAttributes). That distinction is load-bearing for the CLI step: in ALIAS
            # mode Cognito rejects a username of email format, so `admin-create-user --username
            # someone@example.com` would fail. In this mode the email IS the username.
            sign_in_aliases=cognito.SignInAliases(email=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            # Wrong email and wrong password return the same generic failure.
            # One-click uninstall: the whole gate disappears with `cdk destroy`, and with the
            # code deletion at go-live.
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Public client (no secret): the widget is JavaScript in a browser, so a client secret
        # would be readable by anyone who views source - it would be theatre, and Cognito refuses
        # the unsigned browser call anyway unless the client is public.
        #
        # USER_PASSWORD_AUTH, not SRP: the widget does one unsigned fetch to cognito-idp with no
        # SDK and no build step. SRP needs big-integer crypto that no dependency-free widget is
        # going to carry. The tradeoff is that the password crosses the wire (inside TLS) instead
        # of never leaving the browser; for a shared demo account behind a link, that is the right
        # trade. Do not read this as a pattern for real student accounts.
        demo_auth_client = demo_auth_pool.add_client(
            "DemoAuthClient",
            user_pool_client_name="gavilan-library-demo-widget",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True),
            # One day, matching "one sign-in covers the session". This is also Cognito's maximum
            # for an access token. The widget keeps the token in a variable and nothing else, so
            # a reload signs in again regardless of what the token says.
            access_token_validity=Duration.days(1),
            # The widget never uses the refresh token it is handed. Pinning refresh validity to
            # the access token's lifetime means the copy it throws away cannot outlive the
            # session by the 30-day default.
            refresh_token_validity=Duration.days(1),
            id_token_validity=Duration.days(1),
            prevent_user_existence_errors=True,
            disable_o_auth=True,
        )

        # HTTP API (API Gateway v2), NOT REST: ~71% cheaper for a Lambda-proxy job and we
        # need none of the REST-only features.
        #
        # CORS is locked to the origins in config.yaml (cors.allow_origins) - the library site
        # plus a dev localhost entry - never "*". Note what this is and isn't: CORS is enforced
        # by browsers only, so it is not a security boundary (curl/scripts ignore it) and the
        # stage throttling below remains the real cost cap. It does stop a third-party page
        # from driving this billable endpoint from its visitors' browsers.
        #
        # The demo site's own CloudFront origin is appended when it is enabled: it runs the real
        # cross-origin embed, so without it the browser blocks every /query and /warm call from
        # the demo page. It is a deploy-time token (Fn::GetAtt on the distribution), NOT a
        # hardcoded domain, and it disappears from the allowlist when demo_site.enabled is false.
        # This is "add the real origin", not a widening: still an explicit list, still never "*".
        #
        # Methods cover the real routes: POST (/query), GET (/warm), and OPTIONS (the preflight
        # the gateway answers itself).
        #
        # Authorization is in allow_headers because /query is gated (below). The widget sets that
        # header explicitly from JavaScript, which makes every /query a preflighted request: leave
        # it out and the browser fails at the OPTIONS, before the POST is ever sent, and the
        # symptom is a CORS error rather than a 401. AllowCredentials still stays OFF - that flag
        # is about cookies and browser-managed HTTP auth, neither of which is in play for a
        # header this code sets by hand.
        http_api = apigwv2.HttpApi(
            self,
            "ChatbotHttpApi",
            api_name="gavilan-library-chatbot",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=cors_allow_origins + demo_origins,
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["Content-Type", "Authorization"],
            ),
        )
        # One Lambda integration, reused for both routes. HttpLambdaIntegration defaults to
        # payload format version 2.0.
        query_integration = apigwv2_integrations.HttpLambdaIntegration(
            "QueryIntegration", query_lambda
        )
        # Native JWT authorizer - no authorizer Lambda, so nothing to cold-start, nothing to pay
        # for, and no code of ours in the auth decision. API Gateway fetches the pool's JWKS and
        # validates signature, issuer, audience and expiry itself.
        #
        # AUDIENCE IS THE APP CLIENT ID, and that works because of a documented quirk: a Cognito
        # ACCESS token carries no `aud` claim, it carries `client_id`. API Gateway "validates
        # client_id only if aud is not present" (Control access to HTTP APIs with JWT
        # authorizers), so the access token the widget sends validates against this exact entry.
        # An ID token would match on `aud` instead; the widget deliberately sends the access
        # token and never the ID token, which carries the account's attributes for no benefit.
        demo_auth_authorizer = apigwv2_authorizers.HttpJwtAuthorizer(
            "DemoAuthAuthorizer",
            f"https://cognito-idp.{self.region}.amazonaws.com/{demo_auth_pool.user_pool_id}",
            jwt_audience=[demo_auth_client.user_pool_client_id],
            authorizer_name="gavilan-library-demo-access",
            identity_source=["$request.header.Authorization"],
        )
        # /query ONLY. This is the one billable route: every Bedrock call, every guardrail unit
        # and every Primo lookup hangs off it.
        http_api.add_routes(
            path="/query",
            methods=[apigwv2.HttpMethod.POST],
            integration=query_integration,
            authorizer=demo_auth_authorizer,
        )
        # Lightweight pre-warm route: the widget pings GET /warm on load to warm the query
        # Lambda (retrieve-only) before the student's first query. (S3 Vectors itself has no
        # cluster to wake; this just avoids a cold Lambda on the first real query.)
        #
        # DELIBERATELY UNGATED. It fires on page load, before anyone could have signed in, so
        # gating it would only guarantee a cold first query. What it costs is one KB Retrieve -
        # no generation call, no guardrail units, none of the ~10,700 input tokens a /query
        # carries - so it is not the spend this gate exists to stop. Same for /feedback below:
        # an SNS publish is not Bedrock spend, and putting a report path behind a shared
        # password would just lose reports.
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

        # --- Feedback path: SNS topic + email subscription + Lambda + POST /feedback ----
        #
        # "It said something wrong, how do we fix it?" The fix is RAG-first (D-20260727-10): edit
        # the library webpage and the next scheduled scrape of that page's tier corrects the bot.
        # So the value of this
        # path is entirely in telling a librarian WHICH PAGE to edit - the notification carries the
        # source URLs the reported answer cited.
        #
        # SNS, NOT SES. SES starts every account in a sandbox restricted to pre-verified addresses
        # and needs a support request to leave it, which would land on the client at handoff and
        # break one-click install into a fresh account (verified on the deploy account 2026-07-29:
        # ProductionAccessEnabled false). An SNS email subscription needs one confirmation click by
        # the recipient and nothing else.
        #
        # NO SERVER-SIDE STORE: no table, no bucket, no logged copy. The email is the record.
        #
        # NOTHING IS CREATED unless there is somewhere to send: no topic, no subscription, no
        # Lambda, no route. An endpoint that accepts reports with no destination loses them
        # silently, which is worse than not having one - see resolve_feedback for the three cases.
        feedback_url = None
        if feedback_cfg["provision"]:
            # Standard topic; the destination address is its ONLY subscription. enforce_ssl adds a
            # topic policy denying non-TLS publishes, matching the buckets' posture. No KMS: the
            # message lands unencrypted in a mailbox either way, so encrypting it at rest inside
            # SNS protects nothing while adding a key to the one-click install.
            feedback_topic = sns.Topic(
                self,
                "FeedbackTopic",
                display_name="Gavilan Library chatbot",
                enforce_ssl=True,
            )
            # The recipient from config.yaml. CloudFormation creates the subscription in
            # PendingConfirmation and SNS emails a confirmation link; until someone clicks it,
            # publishes succeed and nothing is delivered. That is inherent to SNS email and is
            # exactly the one-time step SES production access would have replaced with a ticket.
            feedback_topic.add_subscription(
                sns_subs.EmailSubscription(feedback_cfg["email"])
            )

            # Its OWN role and its OWN function, not a third route on the query Lambda: this code
            # needs sns:Publish and nothing else, and the query role can already invoke Bedrock and
            # read the KB. Keeping them apart also keeps the student's free text out of a process
            # that logs guardrail assessments and query diagnostics.
            feedback_lambda_role = iam.Role(
                self,
                "FeedbackFunctionRole",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                description="Execution role for the feedback Lambda (publish one SNS email).",
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name(
                        "service-role/AWSLambdaBasicExecutionRole"
                    )
                ],
            )
            feedback_topic.grant_publish(feedback_lambda_role)

            feedback_log_group = logs.LogGroup(
                self,
                "FeedbackFunctionLogGroup",
                retention=logs.RetentionDays.THREE_MONTHS,
                removal_policy=RemovalPolicy.DESTROY,
            )

            feedback_lambda = _lambda.Function(
                self,
                "FeedbackFunction",
                runtime=_LAMBDA_PYTHON,
                handler="feedback_handler.lambda_handler",
                # ONLY feedback_handler.py: it shares app/ with the query handler but needs none
                # of the query handler's prompt, seed catalog or link table, and bundling them
                # would tie this function's asset hash to changes it does not care about.
                code=_lambda.Code.from_asset(
                    str(_APP_DIR), exclude=["*", "!feedback_handler.py"]
                ),
                role=feedback_lambda_role,
                # One validate + one SNS publish. Nothing to retrieve, nothing to generate.
                timeout=Duration.seconds(10),
                memory_size=128,
                log_group=feedback_log_group,
                environment={
                    "FEEDBACK_TOPIC_ARN": feedback_topic.topic_arn,
                    # Size caps from config.yaml. Feedback text never reaches a model, so the
                    # guardrails do not screen it and these caps are the controls that exist.
                    "FEEDBACK_MAX_COMMENT_CHARS": str(feedback_cfg["max_comment_chars"]),
                    "FEEDBACK_MAX_BODY_BYTES": str(feedback_cfg["max_body_bytes"]),
                    "FEEDBACK_MAX_SOURCES": str(feedback_cfg["max_sources"]),
                },
            )

            # Same HTTP API, so /feedback inherits the SAME CORS allowlist as /query and /warm
            # (cors_preflight is configured on the API, not per route) and the SAME stage
            # throttling. Nothing here names an origin.
            http_api.add_routes(
                path="/feedback",
                methods=[apigwv2.HttpMethod.POST],
                integration=apigwv2_integrations.HttpLambdaIntegration(
                    "FeedbackIntegration", feedback_lambda
                ),
            )
            feedback_url = f"{http_api.api_endpoint}/feedback"

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

        # --- Shared URLs (outputs + the demo page below) -------------------------------

        # http_api.api_endpoint has NO trailing slash; the POST route is /query. The
        # CloudFront domain has no scheme, so widget.js is served at https://<domain>/widget.js.
        widget_src = f"https://{widget_distribution.distribution_domain_name}/widget.js"
        query_url = f"{http_api.api_endpoint}/query"

        # --- Demo site (2/2): the page, stamped with both URLs at deploy time ----------

        # frontend/demo-site.html carries two placeholders; they are replaced with CDK tokens
        # here, and s3deploy.Source.data resolves those tokens DURING DEPLOYMENT (it stages the
        # file with substitution markers and the deployment custom resource fills them in). That
        # is what makes the demo endpoint-discovering rather than hand-stamped: nothing in the
        # committed HTML names an account, a region, an API id, or a CloudFront domain.
        #
        # The page then loads the PRODUCTION widget.js from the PRODUCTION CloudFront domain and
        # POSTs to the same /query - it is the real embed, not a copy, so it cannot drift from
        # what the library would paste on their own page.
        if demo_site_enabled:
            demo_html = (_FRONTEND_DIR / _DEMO_PAGE_FILE).read_text(encoding="utf-8")
            missing = [
                token
                for token in (
                    _DEMO_API_URL_TOKEN,
                    _DEMO_WIDGET_SRC_TOKEN,
                    _DEMO_COST_MODEL_TOKEN,
                    _DEMO_USER_POOL_ID_TOKEN,
                    _DEMO_CLIENT_ID_TOKEN,
                )
                if token not in demo_html
            ]
            if missing:
                # Fail the synth rather than ship a demo page whose widget tag points at a
                # literal "__API_URL__" and silently does nothing.
                raise ValueError(
                    f"frontend/{_DEMO_PAGE_FILE} is missing the deploy-time placeholder(s) "
                    f"{missing}. The stack stamps the API and widget URLs into those exact "
                    "strings; renaming them here without updating infra_stack.py would deploy "
                    "a demo page that cannot reach the backend."
                )
            # The cost model goes in first: it is a JSON literal that legitimately contains
            # dollar-sign-free numbers only, but substituting it last would risk a rate value
            # ever containing one of the other placeholders' text. json.dumps with sorted keys
            # keeps the stamped page byte-stable across synths, so an unchanged config does not
            # produce a spurious asset diff.
            demo_html = demo_html.replace(
                _DEMO_COST_MODEL_TOKEN,
                json.dumps(config.get("cost_model", {}), sort_keys=True),
            )
            demo_html = (
                demo_html.replace(_DEMO_WIDGET_SRC_TOKEN, widget_src)
                .replace(_DEMO_API_URL_TOKEN, query_url)
                # The demo page is the thing the gate exists for, so its embed carries the
                # sign-in ids. Discovered, never hardcoded - same reason as the two URLs.
                .replace(_DEMO_USER_POOL_ID_TOKEN, demo_auth_pool.user_pool_id)
                .replace(_DEMO_CLIENT_ID_TOKEN, demo_auth_client.user_pool_client_id)
            )
            # Its own bucket, so the default prune (`aws s3 sync --delete`) is scoped to the demo
            # and can never reach widget.js. distribution_paths invalidates the page (and only
            # this distribution) so a redeploy is visible immediately instead of edge-cached.
            s3deploy.BucketDeployment(
                self,
                "DemoSiteDeployment",
                destination_bucket=demo_bucket,
                sources=[s3deploy.Source.data("index.html", demo_html)],
                distribution=demo_distribution,
                distribution_paths=["/*"],
            )

        # --- Outputs: ready-to-paste embed tag + raw domain / API URL ------------------
        #
        # The embed carries the sign-in ids because /query is gated: a tag without them renders a
        # widget that cannot obtain a token and gets a 401 on every question. Both ids are public
        # by design (they identify the pool, they do not open it) - the password is the secret,
        # and it lives only in the captain's head and Cognito.
        #
        # THESE THREE ATTRIBUTES COME OFF TOGETHER at go-live: delete the auth section above,
        # drop data-user-pool-id/data-client-id here and in frontend/demo-site.html.
        embed_tag = (
            f'<script src="{widget_src}" data-api-url="{query_url}"'
            f' data-user-pool-id="{demo_auth_pool.user_pool_id}"'
            f' data-client-id="{demo_auth_client.user_pool_client_id}" defer></script>'
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
        # --- Sign-in gate: the ids, and the two commands that create the one account ---
        #
        # NEEDS THE DEPLOYER'S OWN AWS CREDENTIALS. CDK cannot do this step: creating the user
        # here would mean a password in the template, and a password in a template is a password
        # in the console, the changeset and the stack events. So the stack prints the commands
        # and a human runs them once.
        CfnOutput(
            self,
            "DemoAuthUserPoolId",
            value=demo_auth_pool.user_pool_id,
            description="Cognito user pool gating POST /query (temporary demo access).",
        )
        CfnOutput(
            self,
            "DemoAuthClientId",
            value=demo_auth_client.user_pool_client_id,
            description="Cognito app client id the widget signs in with (public, no secret).",
        )
        CfnOutput(
            self,
            "DemoAuthCreateUserCommand",
            value=(
                "aws cognito-idp admin-create-user"
                f" --region {self.region}"
                f" --user-pool-id {demo_auth_pool.user_pool_id}"
                " --username YOU@EXAMPLE.COM"
                " --user-attributes Name=email,Value=YOU@EXAMPLE.COM"
                " Name=email_verified,Value=true"
                " --message-action SUPPRESS"
            ),
            description=(
                "Step 1 of 2, run once after deploy with your own AWS credentials. "
                "SUPPRESS skips the invite email; step 2 sets the password."
            ),
        )
        CfnOutput(
            self,
            "DemoAuthSetPasswordCommand",
            value=(
                "aws cognito-idp admin-set-user-password"
                f" --region {self.region}"
                f" --user-pool-id {demo_auth_pool.user_pool_id}"
                " --username YOU@EXAMPLE.COM"
                " --password 'CHOOSE-A-PASSWORD' --permanent"
            ),
            description=(
                "Step 2 of 2. --permanent is REQUIRED: without it the account stays in "
                "FORCE_CHANGE_PASSWORD and sign-in returns a NEW_PASSWORD_REQUIRED challenge "
                "instead of a token, which the widget can only report as a failed sign-in."
            ),
        )
        CfnOutput(
            self,
            "KnowledgeBaseId",
            value=knowledge_base.attr_knowledge_base_id,
            description="Bedrock Knowledge Base id (for eval/eval_config.yaml).",
        )
        # Feedback: either the endpoint the widget will POST to, or - for the one case that is a
        # mistake rather than a choice (enabled with no address) - a line in the deploy output
        # saying why there is no endpoint. A silently absent feature is how this ships broken.
        if feedback_url:
            CfnOutput(
                self,
                "FeedbackApiUrl",
                value=feedback_url,
                description=(
                    "HTTP API POST /feedback endpoint. The recipient must click the SNS "
                    "subscription confirmation email once before anything is delivered."
                ),
            )
        elif feedback_cfg["status"]:
            CfnOutput(
                self,
                "FeedbackStatus",
                value=feedback_cfg["status"],
                description="Why no /feedback endpoint was created.",
            )
        if demo_site_enabled:
            CfnOutput(
                self,
                "DemoSiteUrl",
                value=f"https://{demo_distribution.distribution_domain_name}/",
                description="Shareable demo page: the widget embedded in a sample library site.",
            )
