import copy
import re

import aws_cdk as core
import aws_cdk.assertions as assertions

from infra.config import load_config
from infra.infra_stack import GavilanChatbotStack

CONFIG = load_config()


def _template():
    app = core.App()
    stack = GavilanChatbotStack(app, "GavilanChatbotStack", config=CONFIG)
    return assertions.Template.from_stack(stack)


def _one(template, res_type):
    """Return the single resource of res_type (fails if there isn't exactly one)."""
    (res,) = template.find_resources(res_type).values()
    return res


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
    # Exactly one query Lambda, identified by its handler. Other AWS::Lambda::Function
    # resources exist for the S3 BucketDeployment + auto-delete-objects custom resources,
    # so scope the count to the query function rather than counting all functions.
    query_funcs = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Handler": "handler.lambda_handler"}},
    )
    assert len(query_funcs) == 1, list(query_funcs)
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

    # Exactly one KB role (assumed by the Bedrock service).
    kb_roles = [
        rid for rid, r in roles.items()
        if "bedrock.amazonaws.com" in assumed_services(r)
    ]
    assert len(kb_roles) == 1, kb_roles

    # The query Lambda's role is the one attached to the handler.lambda_handler function.
    # (Other lambda-assumed roles exist for the S3 deployment + auto-delete helpers, so
    # identify the query role via its function rather than by counting lambda roles.)
    query_funcs = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Handler": "handler.lambda_handler"}},
    )
    (query_func,) = query_funcs.values()
    query_role_id = query_func["Properties"]["Role"]["Fn::GetAtt"][0]

    assert "lambda.amazonaws.com" in assumed_services(roles[query_role_id])
    assert query_role_id != kb_roles[0]

    # The query role grants Retrieve + InvokeModel but NOT the KB's aoss actions.
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
                "Roles": [{"Ref": query_role_id}],
            }
        ),
    )


# --- Widget hosting: S3 + CloudFront (OAC) + BucketDeployment --------------------


def test_widget_bucket_is_private_and_oac_ready():
    template = _template()
    # Exactly one S3 bucket (the widget bucket): all public access blocked, and ACLs
    # disabled via bucket-owner-enforced ownership (the ownership OAC requires).
    template.resource_count_is("AWS::S3::Bucket", 1)
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "OwnershipControls": {
                "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
            },
        },
    )


def test_widget_distribution_uses_oac_not_oai():
    template = _template()
    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    # OAC is the required mechanism: an OriginAccessControl resource must exist and there
    # must be NO legacy Origin Access Identity.
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
    template.resource_count_is("AWS::CloudFront::CloudFrontOriginAccessIdentity", 0)

    dist = _one(template, "AWS::CloudFront::Distribution")
    origin = dist["Properties"]["DistributionConfig"]["Origins"][0]
    # The origin references the OAC and does NOT use an OAI (empty OriginAccessIdentity).
    assert "OriginAccessControlId" in origin, origin
    assert origin.get("S3OriginConfig", {}).get("OriginAccessIdentity", "") == ""


def test_widget_bucket_deployment_uploads_the_widget():
    template = _template()
    # The BucketDeployment renders as a CDK bucket-deployment custom resource.
    template.resource_count_is("Custom::CDKBucketDeployment", 1)


def test_stack_outputs_ready_to_paste_embed_tag():
    template = _template()
    outputs = template.find_outputs("*")
    assert "WidgetEmbedTag" in outputs, list(outputs)
    assert "WidgetCdnDomain" in outputs
    assert "ChatbotApiUrl" in outputs

    # The embed tag is a full <script ... data-api-url="..." defer></script> snippet,
    # assembled from the CloudFront domain and the API endpoint via Fn::Join. Check the
    # literal fragments that surround the two injected tokens.
    value = outputs["WidgetEmbedTag"]["Value"]
    literals = "".join(p for p in value["Fn::Join"][1] if isinstance(p, str))
    assert literals.startswith('<script src="https://'), literals
    assert "/widget.js" in literals
    assert 'data-api-url="' in literals
    assert "/query" in literals
    assert literals.endswith("defer></script>"), literals


# --- Bedrock Guardrails: input screen + output backstop -------------------------


def _guardrail_by_name(template, name):
    for res in template.find_resources("AWS::Bedrock::Guardrail").values():
        if res["Properties"].get("Name") == name:
            return res
    raise AssertionError(f"no guardrail named {name}")


def _filters(guardrail):
    return {
        f["Type"]: f
        for f in guardrail["Properties"]["ContentPolicyConfig"]["FiltersConfig"]
    }


def _version_descriptions(template):
    versions = template.find_resources("AWS::Bedrock::GuardrailVersion")
    return sorted(v["Properties"]["Description"] for v in versions.values())


def test_two_guardrails_created_input_and_output():
    template = _template()
    gr = CONFIG["guardrail"]
    # An input screen and an output backstop, each with its blocked message.
    template.resource_count_is("AWS::Bedrock::Guardrail", 2)
    _guardrail_by_name(template, gr["input_name"])
    _guardrail_by_name(template, gr["output_name"])


def test_input_guardrail_screens_content_and_masks_all_pii():
    template = _template()
    gr = CONFIG["guardrail"]
    input_gr = _guardrail_by_name(template, gr["input_name"])
    props = input_gr["Properties"]
    assert props["BlockedInputMessaging"] == gr["blocked_input_messaging"]

    filters = _filters(input_gr)
    # Content filters keep their configured strengths; PROMPT_ATTACK is INPUT-only.
    assert filters["HATE"]["InputStrength"] == "HIGH"
    assert filters["HATE"]["OutputStrength"] == "HIGH"
    assert filters["PROMPT_ATTACK"]["InputStrength"] == "HIGH"
    assert filters["PROMPT_ATTACK"]["OutputStrength"] == "NONE"

    # Every PII entity is ANONYMIZE (mask-and-proceed); none BLOCK, so a masked query always
    # proceeds and the mask-vs-block decision is unambiguous.
    entities = props["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]
    assert len(entities) == len(gr["pii_anonymize_entities"])
    assert {e["Type"] for e in entities} == set(gr["pii_anonymize_entities"])
    assert {e["Action"] for e in entities} == {"ANONYMIZE"}


def test_output_guardrail_is_output_only_content_filters_with_no_pii():
    template = _template()
    gr = CONFIG["guardrail"]
    output_gr = _guardrail_by_name(template, gr["output_name"])
    props = output_gr["Properties"]
    assert props["BlockedOutputsMessaging"] == gr["blocked_outputs_messaging"]

    filters = _filters(output_gr)
    # PROMPT_ATTACK (input-only) is dropped; remaining filters screen OUTPUT only, so the
    # retrieved <context> in the user message is never screened.
    assert "PROMPT_ATTACK" not in filters
    assert filters["HATE"]["InputStrength"] == "NONE"
    assert filters["HATE"]["OutputStrength"] == "HIGH"
    for f in filters.values():
        assert f["InputStrength"] == "NONE"

    # No PII policy at all: masking the answer would re-break contact answers (finding 2.1).
    assert "SensitiveInformationPolicyConfig" not in props


def test_two_guardrail_versions_wired_to_query_lambda():
    template = _template()
    template.resource_count_is("AWS::Bedrock::GuardrailVersion", 2)
    # The query Lambda receives BOTH guardrail id/version pairs + trace via env vars.
    template.has_resource_properties(
        "AWS::Lambda::Function",
        assertions.Match.object_like(
            {
                "Handler": "handler.lambda_handler",
                "Environment": {
                    "Variables": assertions.Match.object_like(
                        {
                            "INPUT_GUARDRAIL_ID": assertions.Match.any_value(),
                            "INPUT_GUARDRAIL_VERSION": assertions.Match.any_value(),
                            "OUTPUT_GUARDRAIL_ID": assertions.Match.any_value(),
                            "OUTPUT_GUARDRAIL_VERSION": assertions.Match.any_value(),
                            "GUARDRAIL_TRACE": "enabled",
                        }
                    )
                },
            }
        ),
    )


def test_guardrail_version_description_is_a_config_content_hash():
    # Finding 1.1: the version description must be a content hash of the resolved guardrail
    # config, not a fixed literal, so any config change publishes a new immutable version.
    template = _template()
    descs = _version_descriptions(template)
    assert len(descs) == 2
    for d in descs:
        assert re.match(r"^(input|output) config-[0-9a-f]{12}$", d), d
    # The two guardrails hash different configs -> different digests.
    assert len(set(descs)) == 2


def test_guardrail_config_change_forces_new_version_description():
    # The core of finding 1.1: editing the guardrail config changes the version description
    # (the ONLY property that changes), which is what forces CloudFormation to publish a new
    # numbered version rather than silently no-op'ing and leaving the Lambda on the stale one.
    base_descs = _version_descriptions(_template())

    mutated = copy.deepcopy(CONFIG)
    # Flip one filter strength; nothing else.
    mutated["guardrail"]["content_filters"][0]["input_strength"] = "LOW"
    app = core.App()
    stack = GavilanChatbotStack(app, "MutatedGuardrail", config=mutated)
    mutated_descs = _version_descriptions(assertions.Template.from_stack(stack))

    assert base_descs != mutated_descs


def test_query_lambda_role_can_apply_both_guardrails():
    template = _template()
    # The query role must be granted bedrock:ApplyGuardrail (the standalone input screen and
    # the Converse-attached output backstop both need it), scoped to two guardrail ARNs.
    apply_stmts = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            if stmt.get("Action") == "bedrock:ApplyGuardrail":
                apply_stmts.append(stmt)
    assert len(apply_stmts) == 1, apply_stmts
    resource = apply_stmts[0]["Resource"]
    # Two ARNs: one per guardrail (each a GetAtt token to the guardrail's ARN attribute).
    assert isinstance(resource, list) and len(resource) == 2, resource
