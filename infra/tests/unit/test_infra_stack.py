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
