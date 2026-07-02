"""Gavilan Library Chatbot infrastructure stack.

Phase 0/1 foundation, all L1 Cfn* from aws-cdk-lib core (see docs/architecture.md).
This stack now stands up the full vector store and the Bedrock Knowledge Base:

  encryption policy + network policy  ->  OpenSearch Serverless collection
  KB execution role + data access policy
  vector index (knn_vector, Titan v2 = 1024 dims)
  Bedrock Knowledge Base (VECTOR, OpenSearch Serverless storage)

NOT wired here yet (later tasks): the Web Crawler data source, the Lambda, the API
Gateway, and the widget. This ends at a KB that synths clean and is ready to attach a
data source to.

API shapes (CfnIndex mappings/settings, CfnKnowledgeBase storage + field mapping, aoss
policy JSON) were verified against aws-cdk-lib 2.260.0 by introspecting the installed
package, not from memory.
"""

import json

from aws_cdk import (
    Stack,
    aws_bedrock as bedrock,
    aws_iam as iam,
    aws_opensearchserverless as oss,
)
from constructs import Construct

# Vector index field names. These MUST match between the CfnIndex mappings (what the
# index physically contains) and the Knowledge Base field_mapping (what Bedrock writes
# to / reads from). They are the Bedrock console defaults, kept for compatibility.
VECTOR_FIELD = "bedrock-knowledge-base-default-vector"
TEXT_FIELD = "AMAZON_BEDROCK_TEXT_CHUNK"
METADATA_FIELD = "AMAZON_BEDROCK_METADATA"
INDEX_NAME = "bedrock-knowledge-base-default-index"

# Titan Text Embeddings v2 -> 1024-dimension vectors.
EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
VECTOR_DIMENSION = 1024


class GavilanChatbotStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # OSS names must be lowercase and <= 32 chars. This backs the Bedrock KB vector
        # store (NextGen, scale-to-zero).
        collection_name = "gavilan-library-kb"

        embedding_model_arn = (
            f"arn:{self.partition}:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL_ID}"
        )

        # --- Security policies for the collection -------------------------------------

        # Encryption: a VECTORSEARCH collection is invalid without one. AWS-owned key for
        # v1; revisit if the sponsor requires a customer-managed KMS key.
        encryption_policy = oss.CfnSecurityPolicy(
            self,
            "CollectionEncryptionPolicy",
            name="gavilan-library-kb-enc",
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
        # layer we do not need here.
        network_policy = oss.CfnSecurityPolicy(
            self,
            "CollectionNetworkPolicy",
            name="gavilan-library-kb-net",
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
            name="gavilan-library-kb-data",
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

        # knn_vector field at 1024 dims (Titan v2), plus a text chunk field and a stored
        # (non-indexed) metadata field. Field names come from the module constants and MUST
        # match the KB field_mapping below.
        vector_index = oss.CfnIndex(
            self,
            "VectorIndex",
            collection_endpoint=collection.attr_collection_endpoint,
            index_name=INDEX_NAME,
            mappings=oss.CfnIndex.MappingsProperty(
                properties={
                    VECTOR_FIELD: oss.CfnIndex.PropertyMappingProperty(
                        type="knn_vector",
                        dimension=VECTOR_DIMENSION,
                        method=oss.CfnIndex.MethodProperty(
                            name="hnsw",
                            engine="faiss",
                            space_type="l2",
                            parameters=oss.CfnIndex.ParametersProperty(
                                ef_construction=512,
                                m=16,
                            ),
                        ),
                    ),
                    TEXT_FIELD: oss.CfnIndex.PropertyMappingProperty(type="text"),
                    METADATA_FIELD: oss.CfnIndex.PropertyMappingProperty(
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
            name="gavilan-library-kb",
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
                    vector_index_name=INDEX_NAME,
                    field_mapping=bedrock.CfnKnowledgeBase.OpenSearchServerlessFieldMappingProperty(
                        vector_field=VECTOR_FIELD,
                        text_field=TEXT_FIELD,
                        metadata_field=METADATA_FIELD,
                    ),
                ),
            ),
        )
        # The KB must not be created before the index exists, and needs its role (and the
        # role's inline policy) in place. The index already depends on the data access
        # policy, so that ordering is transitive.
        knowledge_base.add_dependency(vector_index)
        knowledge_base.node.add_dependency(kb_role)
