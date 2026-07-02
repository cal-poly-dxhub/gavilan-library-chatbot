"""Gavilan Library Chatbot infrastructure stack.

MINIMAL skeleton (Phase 0). This intentionally contains only the OpenSearch
Serverless vector collection and the encryption policy it requires, so that
`cdk synth` is green on a correct foundation. The Bedrock Knowledge Base, Web
Crawler data source, vector index, Lambda, and API Gateway are NOT wired here
yet. See docs/architecture.md for the phased plan. We build on L1 Cfn* constructs
from aws-cdk-lib core.
"""

import json

from aws_cdk import (
    Stack,
    aws_opensearchserverless as oss,
)
from constructs import Construct


class GavilanChatbotStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Name for the OpenSearch Serverless collection. OSS names must be lowercase
        # and <= 32 chars; this backs the Bedrock KB vector store (NextGen, scale-to-zero).
        collection_name = "gavilan-library-kb"

        # A VECTORSEARCH collection is invalid without an encryption policy, so the
        # minimal-but-correct foundation includes it. AWS-owned key for v1; revisit
        # if the sponsor requires a customer-managed KMS key.
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

        collection = oss.CfnCollection(
            self,
            "VectorCollection",
            name=collection_name,
            type="VECTORSEARCH",
            description="Vector store for the Gavilan Library Bedrock Knowledge Base.",
        )

        # The collection cannot be created before its encryption policy exists.
        collection.add_dependency(encryption_policy)
