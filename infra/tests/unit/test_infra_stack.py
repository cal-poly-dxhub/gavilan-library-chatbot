import copy
import json
import re

import aws_cdk as core
import aws_cdk.assertions as assertions

import pytest

from infra.config import load_config, resolve_cors_allow_origins
from infra.infra_stack import GavilanChatbotStack

CONFIG = load_config()


def _template():
    # Skip asset bundling: the scraper deps layer bundles (pip --platform / Docker) at real synth,
    # which needs no Docker for tests and would be slow/network-bound. The resources still
    # synthesize with placeholder assets, which is all these property assertions inspect.
    app = core.App(context={"aws:cdk:bundling-stacks": []})
    stack = GavilanChatbotStack(app, "GavilanChatbotStack", config=CONFIG)
    return assertions.Template.from_stack(stack)


def _one(template, res_type):
    """Return the single resource of res_type (fails if there isn't exactly one)."""
    (res,) = template.find_resources(res_type).values()
    return res


def _join_literals(value):
    """Concatenate the string fragments of an ARN, whether it is a plain string or an
    Fn::Join of literals + Ref tokens (the token dicts are dropped)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Fn::Join" in value:
        return "".join(p for p in value["Fn::Join"][1] if isinstance(p, str))
    return ""


def _join_refs(value):
    """The set of Ref token names (e.g. AWS::AccountId) inside an Fn::Join ARN."""
    refs = set()
    if isinstance(value, dict) and "Fn::Join" in value:
        for p in value["Fn::Join"][1]:
            if isinstance(p, dict) and "Ref" in p:
                refs.add(p["Ref"])
    return refs


def _query_role_statements(template):
    """The IAM policy statements attached to the query Lambda's execution role."""
    (fn,) = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": "handler.lambda_handler"}}
    ).values()
    role_id = fn["Properties"]["Role"]["Fn::GetAtt"][0]
    statements = []
    for pol in template.find_resources("AWS::IAM::Policy").values():
        if any(r.get("Ref") == role_id for r in pol["Properties"].get("Roles", [])):
            statements.extend(pol["Properties"]["PolicyDocument"]["Statement"])
    return statements


def test_no_opensearch_serverless_resources_remain():
    # The migration to S3 Vectors must leave NOTHING from the OpenSearch Serverless path behind:
    # no collection, no security/access policies, no serverless index.
    template = _template()
    template.resource_count_is("AWS::OpenSearchServerless::Collection", 0)
    template.resource_count_is("AWS::OpenSearchServerless::SecurityPolicy", 0)
    template.resource_count_is("AWS::OpenSearchServerless::AccessPolicy", 0)
    template.resource_count_is("AWS::OpenSearchServerless::Index", 0)


def test_s3_vector_bucket_created():
    template = _template()
    template.resource_count_is("AWS::S3Vectors::VectorBucket", 1)
    template.has_resource_properties(
        "AWS::S3Vectors::VectorBucket",
        {"VectorBucketName": CONFIG["vector_store"]["vector_bucket_name"]},
    )


def test_s3_vector_index_dimension_metric_datatype_and_nonfilterable_keys():
    template = _template()
    template.resource_count_is("AWS::S3Vectors::Index", 1)
    (index,) = template.find_resources("AWS::S3Vectors::Index").values()
    props = index["Properties"]
    assert props["IndexName"] == CONFIG["vector_store"]["index_name"]
    # dimension MUST equal the embedding model's output (Titan v2 = 1024).
    assert props["Dimension"] == CONFIG["knowledge_base"]["vector_dimension"]
    assert props["DataType"] == CONFIG["vector_store"]["data_type"]          # float32
    assert props["DistanceMetric"] == CONFIG["vector_store"]["distance_metric"]  # cosine
    # The non-filterable metadata keys (the ingestion-blocking trap) are set at creation.
    non_filterable = props["MetadataConfiguration"]["NonFilterableMetadataKeys"]
    assert non_filterable == CONFIG["vector_store"]["non_filterable_metadata_keys"]
    # The two Bedrock-internal culprits must be present.
    assert "AMAZON_BEDROCK_TEXT" in non_filterable
    assert "AMAZON_BEDROCK_METADATA" in non_filterable


def test_s3_vector_index_depends_on_bucket():
    # The index lives in the bucket; the bucket must be created first.
    template = _template()
    (index,) = template.find_resources("AWS::S3Vectors::Index").values()
    depends = json.dumps(index.get("DependsOn", []))
    assert "VectorBucket" in depends, depends


def test_knowledge_base_storage_is_s3_vectors_pointing_at_the_index():
    template = _template()
    (kb,) = template.find_resources("AWS::Bedrock::KnowledgeBase").values()
    storage = kb["Properties"]["StorageConfiguration"]
    assert storage["Type"] == "S3_VECTORS"
    s3v = storage["S3VectorsConfiguration"]
    # S3VectorsConfiguration is a oneOf: EITHER IndexArn alone, OR IndexName + VectorBucketArn.
    # We use IndexArn alone; passing all three matches both subschemas and CloudFormation rejects
    # it at validation ("2 subschemas matched instead of one"). Lock that in: IndexArn present
    # (GetAtt on the in-stack index), IndexName + VectorBucketArn absent.
    assert s3v["IndexArn"]["Fn::GetAtt"][0].startswith("VectorIndex"), s3v
    assert "IndexName" not in s3v, s3v
    assert "VectorBucketArn" not in s3v, s3v
    # S3 Vectors has no field mapping (unlike OpenSearch Serverless).
    assert "FieldMapping" not in s3v


def test_knowledge_base_created_after_index():
    # Ordering guard: the KB must depend on the vector index existing.
    template = _template()
    kbs = template.find_resources("AWS::Bedrock::KnowledgeBase")
    (kb,) = kbs.values()
    depends_on = kb.get("DependsOn", [])
    assert any(dep.startswith("VectorIndex") for dep in depends_on), depends_on


def test_s3_data_source_is_the_only_data_source():
    # The KB now ingests from S3, not the managed Web Crawler. Exactly one data source, type S3;
    # no WEB data source remains (the crawler was hard-coupled to OpenSearch Serverless).
    template = _template()
    template.resource_count_is("AWS::Bedrock::DataSource", 1)
    (source,) = template.find_resources("AWS::Bedrock::DataSource").values()
    assert source["Properties"]["DataSourceConfiguration"]["Type"] == "S3", source
    assert "WebConfiguration" not in source["Properties"]["DataSourceConfiguration"], source


def test_s3_data_source_points_at_source_bucket_with_crawler_chunking():
    template = _template()
    (source,) = template.find_resources("AWS::Bedrock::DataSource").values()
    ds_config = source["Properties"]["DataSourceConfiguration"]
    # bucketArn references the source bucket via GetAtt (not a hardcoded ARN).
    bucket_arn = ds_config["S3Configuration"]["BucketArn"]
    assert bucket_arn["Fn::GetAtt"][0].startswith("KnowledgeBaseSourceBucket"), bucket_arn
    # Same FIXED_SIZE chunking the crawler used, from config.
    chunk = source["Properties"]["VectorIngestionConfiguration"]["ChunkingConfiguration"]
    assert chunk["ChunkingStrategy"] == CONFIG["chunking"]["strategy"]
    assert (
        chunk["FixedSizeChunkingConfiguration"]["MaxTokens"]
        == CONFIG["chunking"]["max_tokens"]
    )
    assert (
        chunk["FixedSizeChunkingConfiguration"]["OverlapPercentage"]
        == CONFIG["chunking"]["overlap_percentage"]
    )


def test_data_source_created_after_knowledge_base():
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


def test_warm_route_exists():
    # A lightweight GET /warm route for the widget's on-load pre-warm ping.
    template = _template()
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Route", {"RouteKey": "GET /warm"}
    )


def test_query_and_warm_are_the_only_routes_and_share_one_lambda_integration():
    template = _template()
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    keys = {r["Properties"]["RouteKey"] for r in routes.values()}
    assert keys == {"POST /query", "GET /warm"}, keys
    # Both routes reuse a single Lambda integration (same integration id in each Target).
    integrations = template.find_resources("AWS::ApiGatewayV2::Integration")
    assert len(integrations) == 1, list(integrations)
    (integration_id,) = integrations.keys()
    for route in routes.values():
        target = route["Properties"]["Target"]
        assert integration_id in str(target), target


def test_generation_inference_and_query_limit_wired_to_lambda_env():
    # Findings 3.4 + 2.6: the inference knobs and query length cap reach the Lambda from
    # config.yaml (so edits take effect at runtime, not just on paper).
    template = _template()
    template.has_resource_properties(
        "AWS::Lambda::Function",
        assertions.Match.object_like(
            {
                "Handler": "handler.lambda_handler",
                "Environment": {
                    "Variables": assertions.Match.object_like(
                        {
                            "GENERATION_MAX_TOKENS": str(CONFIG["generation"]["max_tokens"]),
                            "GENERATION_TEMPERATURE": str(CONFIG["generation"]["temperature"]),
                            "MAX_QUERY_CHARS": str(CONFIG["request"]["max_query_chars"]),
                        }
                    )
                },
            }
        ),
    )


def test_cors_allow_origins_locked_to_config_and_never_wildcard():
    # Pre-launch hardening: the browser origin allowlist is the production library site plus a
    # dev-only localhost entry, driven from config.yaml. A wildcard here would let any page on
    # the internet drive this billable endpoint (Bedrock + Primo) from its visitors' browsers.
    assert resolve_cors_allow_origins(CONFIG) == [
        "https://www.gavilan.edu",
        "http://localhost:8000",
    ]
    assert "*" not in resolve_cors_allow_origins(CONFIG)

    # ...and that resolved list is what actually reaches the synthesized API.
    template = _template()
    api = _one(template, "AWS::ApiGatewayV2::Api")
    cors = api["Properties"]["CorsConfiguration"]
    assert cors["AllowOrigins"] == ["https://www.gavilan.edu", "http://localhost:8000"], cors
    assert "*" not in cors["AllowOrigins"], cors
    # Methods must cover the real routes (POST /query, GET /warm) plus the OPTIONS preflight
    # the gateway answers itself; only Content-Type is allowed through.
    assert set(cors["AllowMethods"]) == {"GET", "POST", "OPTIONS"}, cors
    assert cors["AllowHeaders"] == ["Content-Type"], cors
    # No cookies or auth headers are sent, so credentialed CORS stays off.
    assert not cors.get("AllowCredentials", False), cors


def test_cors_config_rejects_wildcard_and_missing_origins():
    # The "never *" rule is enforced at synth, not just by whatever config.yaml happens to say.
    wildcard = copy.deepcopy(CONFIG)
    wildcard["cors"]["allow_origins"] = ["*"]
    with pytest.raises(ValueError, match=r"must not contain"):
        resolve_cors_allow_origins(wildcard)

    # A missing/empty allowlist is a loud failure, never a silent permissive fallback.
    for bad in ({}, {"cors": {}}, {"cors": {"allow_origins": []}}):
        with pytest.raises(ValueError):
            resolve_cors_allow_origins(bad)


def test_http_api_stage_throttled_from_config():
    # Stage-level throttling (rate + burst from config) is the load-bearing cost-abuse
    # control, applied to the default route settings so it covers every route.
    template = _template()
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "DefaultRouteSettings": {
                "ThrottlingRateLimit": CONFIG["http_api"]["throttling_rate_limit"],
                "ThrottlingBurstLimit": CONFIG["http_api"]["throttling_burst_limit"],
            }
        },
    )


def test_query_lambda_has_explicit_log_group_with_retention_and_destroy():
    # An explicit, bounded-retention log group torn down with the stack, that the query
    # function actually writes to (not the implicit never-expiring, orphaned-on-destroy one).
    template = _template()
    (fn,) = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": "handler.lambda_handler"}}
    ).values()
    log_group_ref = fn["Properties"]["LoggingConfig"]["LogGroup"]["Ref"]

    log_groups = template.find_resources("AWS::Logs::LogGroup")
    assert log_group_ref in log_groups, log_group_ref
    query_lg = log_groups[log_group_ref]
    # Bounded retention (3 months = 90 days) and destroyed with the stack.
    assert query_lg["Properties"]["RetentionInDays"] == 90
    assert query_lg.get("DeletionPolicy") == "Delete"


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

    # The query role grants Retrieve + InvokeModel* (InvokeModel* because the generation model
    # is invoked through a cross-region inference profile) but NOT the KB's vector-store actions.
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
                                {"Action": "bedrock:InvokeModel*"}
                            ),
                        ]
                    )
                },
                "Roles": [{"Ref": query_role_id}],
            }
        ),
    )


# --- Generation model grant: cross-region inference profile ---------------------


def test_generation_grant_uses_inference_profile_arns():
    template = _template()
    profile_id = CONFIG["generation"]["model_id"]  # us.anthropic...
    assert profile_id.startswith("us."), "test assumes a us. inference-profile id in config"
    base_id = profile_id.split(".", 1)[1]  # anthropic... (geo prefix stripped)

    invoke = [s for s in _query_role_statements(template) if s.get("Action") == "bedrock:InvokeModel*"]
    assert len(invoke) == 1, invoke
    resources = invoke[0]["Resource"]
    # Exactly three ARNs: the inference profile + a source-region and a routed-region model.
    assert isinstance(resources, list) and len(resources) == 3, resources

    literals = [_join_literals(r) for r in resources]
    refs = [_join_refs(r) for r in resources]

    # 1) inference-profile ARN: account + region scoped, keeps the geo-prefixed profile id.
    profile = [(lit, rf) for lit, rf in zip(literals, refs) if "inference-profile/" in lit]
    assert len(profile) == 1, literals
    prof_lit, prof_refs = profile[0]
    assert prof_lit.endswith(f":inference-profile/{profile_id}"), prof_lit
    assert {"AWS::Partition", "AWS::Region", "AWS::AccountId"} <= prof_refs, prof_refs

    # 2/3) foundation-model ARNs: the BASE model id (geo prefix stripped), empty account.
    fms = [(lit, rf) for lit, rf in zip(literals, refs) if "foundation-model/" in lit]
    assert len(fms) == 2, literals
    for fm_lit, fm_refs in fms:
        assert fm_lit.endswith(f"foundation-model/{base_id}"), fm_lit
        assert f"foundation-model/{profile_id}" not in fm_lit  # NOT the geo-prefixed id
        assert "AWS::AccountId" not in fm_refs, fm_lit  # foundation-model ARNs have no account

    # One FM ARN pins this stack's region (source); the other is region-wildcard (destinations).
    source = [lit for lit, rf in fms if "AWS::Region" in rf]
    wildcard = [lit for lit, rf in fms if "AWS::Region" not in rf and ":bedrock:*::" in lit]
    assert len(source) == 1, fms
    assert len(wildcard) == 1, fms


def test_generation_grant_includes_inference_profile_read_access():
    template = _template()
    read = [
        s for s in _query_role_statements(template)
        if isinstance(s.get("Action"), list)
        and set(s["Action"]) == {"bedrock:GetInferenceProfile", "bedrock:ListInferenceProfiles"}
    ]
    assert len(read) == 1, "expected a single GetInferenceProfile/ListInferenceProfiles statement"
    # ListInferenceProfiles has no resource-level scoping, so this statement must be "*".
    assert read[0]["Resource"] == "*"


def test_query_role_has_no_bare_invoke_model_with_profile_config():
    # With a profile id, the grant is InvokeModel* only - never a bare on-demand InvokeModel
    # statement on the query role (the KB role's InvokeModel on the embedding model is separate).
    template = _template()
    bare = [s for s in _query_role_statements(template) if s.get("Action") == "bedrock:InvokeModel"]
    assert bare == [], bare


def test_bare_on_demand_model_id_falls_back_to_single_foundation_model_grant():
    # Backward-compat branch: a bare id (no geo prefix) grants InvokeModel on ONE region-scoped
    # foundation-model ARN, with no inference-profile ARN and no profile-read statement.
    mutated = copy.deepcopy(CONFIG)
    mutated["generation"]["model_id"] = "anthropic.claude-3-5-haiku-20241022-v1:0"
    app = core.App()
    stack = GavilanChatbotStack(app, "BareModelStack", config=mutated)
    template = assertions.Template.from_stack(stack)
    stmts = _query_role_statements(template)

    invoke = [s for s in stmts if s.get("Action") == "bedrock:InvokeModel"]
    assert len(invoke) == 1, invoke
    resource = invoke[0]["Resource"]
    # A single resource renders as a scalar Fn::Join, not a list.
    assert not isinstance(resource, list), resource
    lit = _join_literals(resource)
    assert "inference-profile" not in lit, lit
    assert lit.endswith("::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0"), lit
    # No inference-profile machinery on the bare-id path.
    assert not any(s.get("Action") == "bedrock:InvokeModel*" for s in stmts)
    assert not any(
        isinstance(s.get("Action"), list) and "bedrock:GetInferenceProfile" in s["Action"]
        for s in stmts
    )


# --- Widget hosting: S3 + CloudFront (OAC) + BucketDeployment --------------------


def test_widget_bucket_is_private_and_oac_ready():
    template = _template()
    # Three S3 buckets now (widget + KB source + catalog), all fully private: public access blocked
    # and ACLs disabled via bucket-owner-enforced ownership (the ownership OAC requires).
    template.resource_count_is("AWS::S3::Bucket", 3)
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


def test_kb_source_bucket_blocks_public_access():
    # The KB source bucket must be fully private (public access blocked) - it will hold scraped
    # library content, never served publicly (the KB reads it, CloudFront does not).
    template = _template()
    buckets = template.find_resources("AWS::S3::Bucket")
    # KB source bucket + widget bucket + catalog bucket (Phase 2b).
    assert len(buckets) == 3, list(buckets)
    for logical_id, res in buckets.items():
        assert res["Properties"]["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }, logical_id


def test_kb_role_can_read_source_bucket():
    # KB execution role must be able to list the source bucket and get its objects, or ingestion
    # from S3 fails. Wired through CDK (bucket ARN + /*), granted on the role's default policy.
    template = _template()
    # Collect S3 read statements across all inline policies (role default policies).
    s3_reads = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt["Action"]
            actions = actions if isinstance(actions, list) else [actions]
            if "s3:GetObject" in actions and "s3:ListBucket" in actions:
                s3_reads.append(stmt)
    assert len(s3_reads) == 1, s3_reads
    resources = s3_reads[0]["Resource"]
    # Two resources: the bucket ARN and its objects (/*), both GetAtt on the source bucket.
    assert len(resources) == 2, resources
    for r in resources:
        # bucket ARN is a GetAtt; objects ARN is a Join of [GetAtt, "/*"].
        blob = json.dumps(r)
        assert "KnowledgeBaseSourceBucket" in blob, r


def test_kb_role_has_s3vectors_dataplane_grant_scoped_to_index_and_no_aoss():
    # The KB role must read/write the S3 Vectors index (the documented data-plane actions), scoped
    # to the index ARN - and must carry NO OpenSearch Serverless (aoss) permissions anymore.
    template = _template()
    all_stmts = [
        stmt
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    s3v_stmts = [
        s for s in all_stmts
        if any(
            str(a).startswith("s3vectors:")
            for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
        )
    ]
    assert len(s3v_stmts) == 1, s3v_stmts
    actions = set(s3v_stmts[0]["Action"])
    assert actions == {
        "s3vectors:PutVectors",
        "s3vectors:GetVectors",
        "s3vectors:DeleteVectors",
        "s3vectors:QueryVectors",
        "s3vectors:GetIndex",
    }, actions
    # Scoped to the vector index ARN (GetAtt), not a wildcard.
    resource = s3v_stmts[0]["Resource"]
    assert json.dumps(resource).count("VectorIndex") >= 1, resource
    assert resource != "*"

    # No aoss action survives anywhere in the template.
    for s in all_stmts:
        acts = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        assert not any(str(a).startswith("aoss:") for a in acts), s


# --- Scraper Lambda: deps layer, function, IAM, EventBridge schedule ----------------------------

def _scraper_function(template):
    """The scraper Lambda, identified by its handler (distinct from the query function)."""
    funcs = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Handler": "lambda_function.handler"}},
    )
    assert len(funcs) == 1, list(funcs)
    return next(iter(funcs.values()))


def test_scraper_lambda_has_deps_layer_and_arch():
    template = _template()
    # A LayerVersion carries the manylinux deps; the function uses it on x86_64 (matching the
    # wheel platform tag), with a multi-minute timeout and a few hundred MB of memory. (Another
    # LayerVersion - the AWS CLI layer - belongs to the widget BucketDeployment; identify ours by
    # description rather than counting all layers.)
    deps_layers = [
        layer
        for layer in template.find_resources("AWS::Lambda::LayerVersion").values()
        if "trafilatura" in (layer["Properties"].get("Description") or "")
    ]
    assert len(deps_layers) == 1, deps_layers
    func = _scraper_function(template)
    props = func["Properties"]
    assert props["Runtime"] == "python3.13"
    assert props["Architectures"] == ["x86_64"]
    assert props["Timeout"] == 300
    assert props["MemorySize"] == 512
    assert len(props["Layers"]) == 1


def test_scraper_lambda_env_wires_bucket_kb_and_data_source():
    template = _template()
    env = _scraper_function(template)["Properties"]["Environment"]["Variables"]
    # seed_urls come from config as a JSON string; bucket/KB/data-source are resource refs.
    assert json.loads(env["SEED_URLS"]) == CONFIG["scraper"]["seed_urls"]
    assert "SOURCE_BUCKET" in env and "KNOWLEDGE_BASE_ID" in env and "DATA_SOURCE_ID" in env


def test_scraper_role_can_write_prune_and_start_ingestion():
    template = _template()
    statements = [
        stmt
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]
    ]

    # Objects: write the fresh documents AND delete the ones the seed list no longer calls for.
    # Without DeleteObject the prune soft-fails and de-seeding a page is a silent no-op - the
    # document stays in the bucket and stays indexed forever.
    writes = [s for s in statements if s["Action"] == ["s3:PutObject", "s3:DeleteObject"]]
    assert len(writes) == 1, writes
    assert "KnowledgeBaseSourceBucket" in json.dumps(writes[0]["Resource"])

    # ListBucket is granted on the BUCKET arn, not the object arn: the prune has to enumerate
    # what is actually there before it can tell what is stale.
    lists = [s for s in statements if s["Action"] == "s3:ListBucket"]
    source_lists = [s for s in lists if "KnowledgeBaseSourceBucket" in json.dumps(s["Resource"])]
    assert len(source_lists) == 1, source_lists
    assert "/*" not in json.dumps(source_lists[0]["Resource"])

    ingest = [s for s in statements if s["Action"] == "bedrock:StartIngestionJob"]
    assert len(ingest) == 1, ingest
    assert "KnowledgeBase" in json.dumps(ingest[0]["Resource"])


def test_scraper_env_carries_the_kb_exclusion_list():
    # databases.php must stay in SEED_URLS (regenerate_catalog parses its HTML) while being kept
    # out of the knowledge base. If these two ever disagree, either the catalog freezes or the
    # largest redundant document in the corpus comes back.
    env = _scraper_function(_template())["Properties"]["Environment"]["Variables"]
    excluded = json.loads(env["KB_EXCLUDE_URLS"])
    assert excluded == CONFIG["scraper"].get("kb_exclude_urls", [])
    for url in excluded:
        assert url in CONFIG["scraper"]["seed_urls"], url


def test_eventbridge_schedule_targets_the_scraper_lambda():
    template = _template()
    template.has_resource_properties(
        "AWS::Events::Rule",
        {"ScheduleExpression": CONFIG["scraper"]["schedule_cron"], "State": "ENABLED"},
    )
    # The rule targets exactly the scraper function (by its logical id via Fn::GetAtt Arn).
    (rule,) = template.find_resources("AWS::Events::Rule").values()
    targets = rule["Properties"]["Targets"]
    assert len(targets) == 1, targets
    assert "ScraperFunction" in json.dumps(targets[0]["Arn"])


def test_install_trigger_invokes_scraper_fire_and_forget():
    # A CDK triggers.Trigger runs the scraper ONCE at deploy so the KB is populated on install.
    # EVENT invocation = fire-and-forget: the deploy dispatches it and never blocks/fails on the
    # scrape result. execute_after wires ordering after the scraper + its write/ingest targets.
    template = _template()
    trigs = template.find_resources("Custom::Trigger")
    assert len(trigs) == 1, list(trigs)
    (trig,) = trigs.values()
    props = trig["Properties"]
    assert props["InvocationType"] == "Event"
    assert "ScraperFunction" in json.dumps(props["HandlerArn"])
    depends = json.dumps(trig.get("DependsOn", []))
    for needle in ("ScraperFunction", "KnowledgeBase", "S3DataSource", "KnowledgeBaseSourceBucket"):
        assert needle in depends, (needle, depends)


def test_install_trigger_invoker_grant_is_scoped_to_the_scraper():
    # The Trigger construct auto-grants its CDK-managed invoker lambda:InvokeFunction, scoped to
    # the scraper function (no manual grant, least-privilege).
    template = _template()
    invoke_stmts = [
        stmt
        for role in template.find_resources("AWS::IAM::Role").values()
        for policy in role["Properties"].get("Policies", [])
        for stmt in policy["PolicyDocument"]["Statement"]
        if "lambda:InvokeFunction"
        in (stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]])
    ]
    assert invoke_stmts, "expected an auto InvokeFunction grant for the trigger invoker"
    assert any("ScraperFunction" in json.dumps(s["Resource"]) for s in invoke_stmts)


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
    assert "KnowledgeBaseId" in outputs

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

    # No PII policy at all: masking the answer would re-break contact answers.
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
    # The version description must be a content hash of the resolved guardrail config, not a
    # fixed literal, so any config change publishes a new immutable version.
    template = _template()
    descs = _version_descriptions(template)
    assert len(descs) == 2
    for d in descs:
        assert re.match(r"^(input|output) config-[0-9a-f]{12}$", d), d
    # The two guardrails hash different configs -> different digests.
    assert len(set(descs)) == 2


def test_guardrail_config_change_forces_new_version_description():
    # Editing the guardrail config changes the version description (the ONLY property that
    # changes), which is what forces CloudFormation to publish a new numbered version rather
    # than silently no-op'ing and leaving the Lambda on the stale one.
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


# --- Phase 2b: self-updating database catalog (bucket, env, IAM) ----------------------------


def _statements_for_handler(template, handler_name):
    """IAM policy statements attached to the execution role of the Lambda with this handler."""
    (fn,) = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": handler_name}}
    ).values()
    role_id = fn["Properties"]["Role"]["Fn::GetAtt"][0]
    stmts = []
    for pol in template.find_resources("AWS::IAM::Policy").values():
        if any(r.get("Ref") == role_id for r in pol["Properties"].get("Roles", [])):
            stmts.extend(pol["Properties"]["PolicyDocument"]["Statement"])
    return stmts


def _env_for_handler(template, handler_name):
    (fn,) = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": handler_name}}
    ).values()
    return fn["Properties"]["Environment"]["Variables"]


def test_dedicated_catalog_bucket_exists():
    # A third private bucket for the catalog, separate from the KB source bucket (so the catalog
    # JSON is never ingested into the vector store).
    template = _template()
    buckets = template.find_resources("AWS::S3::Bucket")
    assert any(lid.startswith("CatalogBucket") for lid in buckets), list(buckets)


def test_scraper_env_wires_catalog():
    template = _template()
    env = _env_for_handler(template, "lambda_function.handler")
    assert "CATALOG_BUCKET" in env  # a Ref/GetAtt to the catalog bucket
    assert env["CATALOG_KEY"] == CONFIG["catalog"]["s3_key"]
    assert env["CATALOG_ENRICHMENT_MODEL_ID"] == CONFIG["catalog"]["enrichment_model_id"]
    assert env["CATALOG_MIN_DATABASES"] == str(CONFIG["catalog"]["min_databases"])


def test_query_env_wires_catalog():
    template = _template()
    env = _env_for_handler(template, "handler.lambda_handler")
    assert "CATALOG_BUCKET" in env
    assert env["CATALOG_KEY"] == CONFIG["catalog"]["s3_key"]
    assert env["CATALOG_CACHE_TTL_SECONDS"] == str(CONFIG["catalog"]["cache_ttl_seconds"])


def test_scraper_role_can_rw_catalog_and_invoke_enrichment_model():
    template = _template()
    stmts = _statements_for_handler(template, "lambda_function.handler")

    # S3 read+write scoped to the catalog object (not "*").
    s3_stmts = [
        s for s in stmts
        if set(s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
        == {"s3:GetObject", "s3:PutObject"}
    ]
    assert len(s3_stmts) == 1, s3_stmts
    assert "CatalogBucket" in json.dumps(s3_stmts[0]["Resource"])

    # Enrichment model invoke: InvokeModel* including the Nova Pro inference profile.
    invoke = [
        s for s in stmts
        if "bedrock:InvokeModel*" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
    ]
    assert invoke, stmts
    # The enrichment model's foundation-model/inference-profile ARN is in the grant.
    base = CONFIG["catalog"]["enrichment_model_id"].split(".", 1)[1]  # drop the "us." geo prefix
    assert any(base in json.dumps(s["Resource"]) for s in invoke)


def test_query_role_can_read_catalog_object():
    template = _template()
    stmts = _statements_for_handler(template, "handler.lambda_handler")
    get_stmts = [
        s for s in stmts
        if (s["Action"] if isinstance(s["Action"], list) else [s["Action"]]) == ["s3:GetObject"]
        and "CatalogBucket" in json.dumps(s["Resource"])
    ]
    assert len(get_stmts) == 1, get_stmts
    # Read-only: no PutObject to the catalog from the query role.
    assert not any(
        "s3:PutObject" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
        and "CatalogBucket" in json.dumps(s.get("Resource"))
        for s in stmts
    )
