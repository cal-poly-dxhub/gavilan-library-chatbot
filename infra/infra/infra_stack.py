"""Gavilan Library Chatbot infrastructure stack.

Phase 0/1 foundation, all L1 Cfn* from aws-cdk-lib core (see docs/architecture.md).
This stack stands up the full vector store, the Bedrock Knowledge Base, and its Web
Crawler data source:

  encryption policy + network policy  ->  OpenSearch Serverless collection
  KB execution role + data access policy
  vector index (knn_vector, Titan v2 = 1024 dims)
  Bedrock Knowledge Base (VECTOR, OpenSearch Serverless storage)
  Web Crawler data source (type WEB, FIXED_SIZE chunking)
  query-path Lambda (own role) + HTTP API (API Gateway v2), POST /query
  widget hosting: private S3 bucket + CloudFront (OAC) + BucketDeployment(widget.js)

All changeable knobs come from the repo-root config.yaml

"""

import json
from pathlib import Path
from typing import Any, Dict

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_bedrock as bedrock,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_opensearchserverless as oss,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
)
from constructs import Construct

# Repo-root app/ directory holding the Lambda handler source (app/handler.py).
# infra_stack.py is <repo>/infra/infra/infra_stack.py, so parents[2] is the repo root.
_APP_DIR = Path(__file__).resolve().parents[2] / "app"
# Repo-root frontend/ directory. Only widget.js is uploaded (mock.js / demo.html are
# dev-only and must never ship to production).
_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


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
        fields = vs_cfg["fields"]
        hnsw = vs_cfg["hnsw"]
        web_cfg = config["data_source"]["web_crawler"]
        chunking_cfg = config["chunking"]
        retrieval_cfg = config["retrieval"]
        generation_cfg = config["generation"]

        kb_name = kb_cfg["name"]
        collection_name = vs_cfg["collection_name"]
        index_name = vs_cfg["index_name"]
        vector_field = fields["vector"]
        text_field = fields["text"]
        metadata_field = fields["metadata"]

        embedding_model_arn = (
            f"arn:{self.partition}:bedrock:{self.region}"
            f"::foundation-model/{kb_cfg['embedding_model_id']}"
        )

        # --- Security policies for the collection -------------------------------------

        # Encryption: a VECTORSEARCH collection is invalid without one. AWS-owned key for
        # v1; revisit if the sponsor requires a customer-managed KMS key.
        encryption_policy = oss.CfnSecurityPolicy(
            self,
            "CollectionEncryptionPolicy",
            name=f"{collection_name}-enc",
            type="encryption",
            policy=json.dumps(
                {
                    "Rules": [
                        {
                            "ResourceType": "collection",
                            "Resource": [f"collection/{collection_name}"],
                        }
                    ],
                    "AWSOwnedKey": True,
                }
            ),
        )

        # Network: allow public (non-VPC) access to the collection and its dashboard.
        # Authorization is enforced by the data access policy + IAM below, NOT by network
        # isolation. This is the AWS-recommended pattern for a Bedrock-backed collection
        # serving public library-website content; VPC isolation is a compliance-only
        # layer not needed here.
        network_policy = oss.CfnSecurityPolicy(
            self,
            "CollectionNetworkPolicy",
            name=f"{collection_name}-net",
            type="network",
            policy=json.dumps(
                [
                    {
                        "Rules": [
                            {
                                "ResourceType": "collection",
                                "Resource": [f"collection/{collection_name}"],
                            },
                            {
                                "ResourceType": "dashboard",
                                "Resource": [f"collection/{collection_name}"],
                            },
                        ],
                        "AllowFromPublic": True,
                    }
                ]
            ),
        )

        collection = oss.CfnCollection(
            self,
            "VectorCollection",
            name=collection_name,
            type="VECTORSEARCH",
            description="Vector store for the Gavilan Library Bedrock Knowledge Base.",
        )
        # The collection cannot be created before its security policies exist.
        collection.add_dependency(encryption_policy)
        collection.add_dependency(network_policy)

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
        # Data-plane access to the collection. aoss:APIAccessAll is the IAM permission AWS
        # requires for a principal to reach OpenSearch API operations on the collection;
        # the fine-grained index/collection actions are granted in the data access policy.
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["aoss:APIAccessAll"],
                resources=[collection.attr_arn],
            )
        )

        # --- Data access policy -------------------------------------------------------

        # Grants the KB role the index + collection data-plane actions it needs to create
        # and populate the vector index. NOTE (deploy-time): the principal that actually
        # creates the CfnIndex is the CloudFormation execution role, so on a real deploy
        # that role must ALSO appear here (or the index create fails). Recorded in CLAUDE.md.
        data_access_policy = oss.CfnAccessPolicy(
            self,
            "CollectionDataAccessPolicy",
            name=f"{collection_name}-data",
            type="data",
            policy=json.dumps(
                [
                    {
                        "Rules": [
                            {
                                "ResourceType": "index",
                                "Resource": [f"index/{collection_name}/*"],
                                "Permission": [
                                    "aoss:CreateIndex",
                                    "aoss:DescribeIndex",
                                    "aoss:ReadDocument",
                                    "aoss:WriteDocument",
                                    "aoss:UpdateIndex",
                                ],
                            },
                            {
                                "ResourceType": "collection",
                                "Resource": [f"collection/{collection_name}"],
                                "Permission": [
                                    "aoss:CreateCollectionItems",
                                    "aoss:DescribeCollectionItems",
                                    "aoss:UpdateCollectionItems",
                                ],
                            },
                        ],
                        # kb_role.role_arn is a token; json.dumps embeds it as a string
                        # placeholder that CDK resolves into the real ARN at synth.
                        "Principal": [kb_role.role_arn],
                    }
                ]
            ),
        )

        # --- Vector index -------------------------------------------------------------

        # knn_vector field at the configured dimension (Titan v2 = 1024), plus a text
        # chunk field and a stored (non-indexed) metadata field. Field names come from
        # config and MUST match the KB field_mapping below.
        vector_index = oss.CfnIndex(
            self,
            "VectorIndex",
            collection_endpoint=collection.attr_collection_endpoint,
            index_name=index_name,
            mappings=oss.CfnIndex.MappingsProperty(
                properties={
                    vector_field: oss.CfnIndex.PropertyMappingProperty(
                        type="knn_vector",
                        dimension=kb_cfg["vector_dimension"],
                        method=oss.CfnIndex.MethodProperty(
                            name="hnsw",
                            engine=hnsw["engine"],
                            space_type=hnsw["space_type"],
                            parameters=oss.CfnIndex.ParametersProperty(
                                ef_construction=hnsw["ef_construction"],
                                m=hnsw["m"],
                            ),
                        ),
                    ),
                    text_field: oss.CfnIndex.PropertyMappingProperty(type="text"),
                    metadata_field: oss.CfnIndex.PropertyMappingProperty(
                        type="text",
                        index=False,
                    ),
                }
            ),
            settings=oss.CfnIndex.IndexSettingsProperty(
                index=oss.CfnIndex.IndexProperty(knn=True)
            ),
        )
        # The index is created via the OSS data plane, which needs the collection to exist
        # and the network + data access policies in place first.
        vector_index.add_dependency(collection)
        vector_index.add_dependency(network_policy)
        vector_index.add_dependency(data_access_policy)

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
                type="OPENSEARCH_SERVERLESS",
                opensearch_serverless_configuration=bedrock.CfnKnowledgeBase.OpenSearchServerlessConfigurationProperty(
                    collection_arn=collection.attr_arn,
                    vector_index_name=index_name,
                    field_mapping=bedrock.CfnKnowledgeBase.OpenSearchServerlessFieldMappingProperty(
                        vector_field=vector_field,
                        text_field=text_field,
                        metadata_field=metadata_field,
                    ),
                ),
            ),
        )
        # The KB must not be created before the index exists, and needs its role (and the
        # role's inline policy) in place. The index already depends on the data access
        # policy, so that ordering is transitive.
        knowledge_base.add_dependency(vector_index)
        knowledge_base.node.add_dependency(kb_role)

        # --- Web Crawler data source --------------------------------------------------

        # Seed URLs and include/exclude regex filters come from config, not literals here.
        # Empty filter lists are omitted (passed as None) rather than emitted as empty
        # arrays. Chunking is FIXED_SIZE for v1 per architecture.md Phase 1.
        seed_urls = [
            bedrock.CfnDataSource.SeedUrlProperty(url=url)
            for url in web_cfg["seed_urls"]
        ]
        web_data_source = bedrock.CfnDataSource(
            self,
            "WebCrawlerDataSource",
            name=f"{kb_name}-web",
            knowledge_base_id=knowledge_base.attr_knowledge_base_id,
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="WEB",
                web_configuration=bedrock.CfnDataSource.WebDataSourceConfigurationProperty(
                    source_configuration=bedrock.CfnDataSource.WebSourceConfigurationProperty(
                        url_configuration=bedrock.CfnDataSource.UrlConfigurationProperty(
                            seed_urls=seed_urls,
                        ),
                    ),
                    crawler_configuration=bedrock.CfnDataSource.WebCrawlerConfigurationProperty(
                        scope=web_cfg.get("scope"),
                        inclusion_filters=web_cfg.get("include_patterns") or None,
                        exclusion_filters=web_cfg.get("exclude_patterns") or None,
                        crawler_limits=bedrock.CfnDataSource.WebCrawlerLimitsProperty(
                            max_pages=web_cfg.get("max_pages"),
                            rate_limit=web_cfg.get("rate_limit"),
                        ),
                    ),
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
        web_data_source.add_dependency(knowledge_base)

        # --- Query path: Lambda + HTTP API --------------------------------------------

        generation_model_arn = (
            f"arn:{self.partition}:bedrock:{self.region}"
            f"::foundation-model/{generation_cfg['model_id']}"
        )

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
        # Invoke the generation model (Converse maps to the InvokeModel action).
        query_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[generation_model_arn],
            )
        )

        query_lambda = _lambda.Function(
            self,
            "QueryFunction",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset(str(_APP_DIR)),
            role=query_lambda_role,
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "KNOWLEDGE_BASE_ID": knowledge_base.attr_knowledge_base_id,
                "GENERATION_MODEL_ID": generation_cfg["model_id"],
                "NUMBER_OF_RESULTS": str(retrieval_cfg["number_of_results"]),
                "BEDROCK_REGION": self.region,
            },
        )
        # The Lambda queries the KB at runtime, so it must not exist before the KB.
        query_lambda.node.add_dependency(knowledge_base)

        # HTTP API (API Gateway v2), NOT REST: ~71% cheaper for a Lambda-proxy job and we
        # need none of the REST-only features. CORS is permissive for now.
        # TODO: lock allow_origins to the library widget domain before launch.
        http_api = apigwv2.HttpApi(
            self,
            "ChatbotHttpApi",
            api_name="gavilan-library-chatbot",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigwv2.CorsHttpMethod.POST, apigwv2.CorsHttpMethod.OPTIONS],
                allow_headers=["Content-Type"],
            ),
        )
        # HttpLambdaIntegration defaults to payload format version 2.0.
        http_api.add_routes(
            path="/query",
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "QueryIntegration", query_lambda
            ),
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
