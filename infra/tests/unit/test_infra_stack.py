import aws_cdk as core
import aws_cdk.assertions as assertions

from infra.config import load_config
from infra.infra_stack import GavilanChatbotStack

CONFIG = load_config()


def _template():
    app = core.App()
    stack = GavilanChatbotStack(app, "GavilanChatbotStack", config=CONFIG)
    return assertions.Template.from_stack(stack)


def test_vector_collection_and_policies_created():
    template = _template()
    template.has_resource_properties(
        "AWS::OpenSearchServerless::Collection",
        {"Name": CONFIG["vector_store"]["collection_name"], "Type": "VECTORSEARCH"},
    )
    # Encryption + network policies.
    template.resource_count_is("AWS::OpenSearchServerless::SecurityPolicy", 2)
    template.resource_count_is("AWS::OpenSearchServerless::AccessPolicy", 1)


def test_vector_index_uses_configured_dimensions():
    template = _template()
    template.has_resource_properties(
        "AWS::OpenSearchServerless::Index",
        {
            "IndexName": CONFIG["vector_store"]["index_name"],
            "Mappings": {
                "Properties": {
                    CONFIG["vector_store"]["fields"]["vector"]: {
                        "Type": "knn_vector",
                        "Dimension": CONFIG["knowledge_base"]["vector_dimension"],
                    }
                }
            },
        },
    )


def test_knowledge_base_field_mapping_matches_index():
    template = _template()
    fields = CONFIG["vector_store"]["fields"]
    # The KB field mapping must reference exactly the fields the index defines.
    template.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase",
        {
            "Name": CONFIG["knowledge_base"]["name"],
            "StorageConfiguration": {
                "Type": "OPENSEARCH_SERVERLESS",
                "OpensearchServerlessConfiguration": {
                    "VectorIndexName": CONFIG["vector_store"]["index_name"],
                    "FieldMapping": {
                        "VectorField": fields["vector"],
                        "TextField": fields["text"],
                        "MetadataField": fields["metadata"],
                    },
                },
            },
        },
    )


def test_knowledge_base_created_after_index():
    # Ordering guard: the KB must depend on the vector index existing.
    template = _template()
    kbs = template.find_resources("AWS::Bedrock::KnowledgeBase")
    (kb,) = kbs.values()
    depends_on = kb.get("DependsOn", [])
    assert any(dep.startswith("VectorIndex") for dep in depends_on), depends_on


def test_web_crawler_data_source_reads_seed_urls_from_config():
    template = _template()
    seed_urls = CONFIG["data_source"]["web_crawler"]["seed_urls"]
    template.has_resource_properties(
        "AWS::Bedrock::DataSource",
        {
            "DataSourceConfiguration": {
                "Type": "WEB",
                "WebConfiguration": {
                    "SourceConfiguration": {
                        "UrlConfiguration": {
                            "SeedUrls": [{"Url": url} for url in seed_urls]
                        }
                    }
                },
            },
            "VectorIngestionConfiguration": {
                "ChunkingConfiguration": {
                    "ChunkingStrategy": CONFIG["chunking"]["strategy"]
                }
            },
        },
    )


def test_web_crawler_data_source_created_after_knowledge_base():
    template = _template()
    sources = template.find_resources("AWS::Bedrock::DataSource")
    (source,) = sources.values()
    depends_on = source.get("DependsOn", [])
    assert any(dep.startswith("KnowledgeBase") for dep in depends_on), depends_on


def test_query_lambda_and_http_api_synthesize():
    template = _template()
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "handler.lambda_handler", "Runtime": "python3.13"},
    )
    template.resource_count_is("AWS::ApiGatewayV2::Api", 1)
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Api", {"ProtocolType": "HTTP"}
    )


def test_post_query_route_exists():
    template = _template()
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Route", {"RouteKey": "POST /query"}
    )


def test_query_lambda_has_its_own_role_distinct_from_kb_role():
    template = _template()
    roles = template.find_resources("AWS::IAM::Role")

    def assumed_services(role):
        statements = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        return {s["Principal"]["Service"] for s in statements}

    lambda_roles = [
        rid for rid, r in roles.items()
        if "lambda.amazonaws.com" in assumed_services(r)
    ]
    kb_roles = [
        rid for rid, r in roles.items()
        if "bedrock.amazonaws.com" in assumed_services(r)
    ]
    # Exactly one Lambda role and one KB role, and they are different resources.
    assert len(lambda_roles) == 1, lambda_roles
    assert len(kb_roles) == 1, kb_roles
    assert lambda_roles[0] != kb_roles[0]

    # The Lambda role grants Retrieve + InvokeModel but NOT the KB's aoss actions.
    template.has_resource_properties(
        "AWS::IAM::Policy",
        assertions.Match.object_like(
            {
                "PolicyDocument": {
                    "Statement": assertions.Match.array_with(
                        [
                            assertions.Match.object_like(
                                {"Action": "bedrock:Retrieve"}
                            ),
                            assertions.Match.object_like(
                                {"Action": "bedrock:InvokeModel"}
                            ),
                        ]
                    )
                },
                "Roles": [{"Ref": lambda_roles[0]}],
            }
        ),
    )
