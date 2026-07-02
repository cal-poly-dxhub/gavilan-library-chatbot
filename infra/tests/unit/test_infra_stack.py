import aws_cdk as core
import aws_cdk.assertions as assertions

from infra.infra_stack import GavilanChatbotStack


def test_vector_collection_created():
    app = core.App()
    stack = GavilanChatbotStack(app, "GavilanChatbotStack")
    template = assertions.Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::OpenSearchServerless::Collection",
        {"Name": "gavilan-library-kb", "Type": "VECTORSEARCH"},
    )
    template.resource_count_is("AWS::OpenSearchServerless::SecurityPolicy", 1)
