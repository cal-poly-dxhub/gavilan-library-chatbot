import aws_cdk as core
import aws_cdk.assertions as assertions

from infra.infra_stack import (
    GavilanChatbotStack,
    INDEX_NAME,
    METADATA_FIELD,
    TEXT_FIELD,
    VECTOR_DIMENSION,
    VECTOR_FIELD,
)


def _template():
    app = core.App()
    stack = GavilanChatbotStack(app, "GavilanChatbotStack")
    return assertions.Template.from_stack(stack)


def test_vector_collection_and_policies_created():
    template = _template()
    template.has_resource_properties(
        "AWS::OpenSearchServerless::Collection",
        {"Name": "gavilan-library-kb", "Type": "VECTORSEARCH"},
    )
    # Encryption + network policies.
    template.resource_count_is("AWS::OpenSearchServerless::SecurityPolicy", 2)
    template.resource_count_is("AWS::OpenSearchServerless::AccessPolicy", 1)


def test_vector_index_uses_titan_dimensions():
    template = _template()
    template.has_resource_properties(
        "AWS::OpenSearchServerless::Index",
        {
            "IndexName": INDEX_NAME,
            "Mappings": {
                "Properties": {
                    VECTOR_FIELD: {
                        "Type": "knn_vector",
                        "Dimension": VECTOR_DIMENSION,
                    }
                }
            },
        },
    )


def test_knowledge_base_field_mapping_matches_index():
    template = _template()
    # The KB field mapping must reference exactly the fields the index defines.
    template.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase",
        {
            "Name": "gavilan-library-kb",
            "StorageConfiguration": {
                "Type": "OPENSEARCH_SERVERLESS",
                "OpensearchServerlessConfiguration": {
                    "VectorIndexName": INDEX_NAME,
                    "FieldMapping": {
                        "VectorField": VECTOR_FIELD,
                        "TextField": TEXT_FIELD,
                        "MetadataField": METADATA_FIELD,
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
