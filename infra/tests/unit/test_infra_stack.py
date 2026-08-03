import copy
import json
import pathlib
import re
from urllib.parse import urlparse

import aws_cdk as core
import aws_cdk.assertions as assertions

import pytest

from infra.config import (
    load_config,
    resolve_cors_allow_origins,
    resolve_feedback,
    resolve_scraper_tiers,
    resolve_seed_urls,
)
from infra.infra_stack import GavilanChatbotStack

CONFIG = load_config()
SCRAPER_TIERS = resolve_scraper_tiers(CONFIG)
SEED_URLS = resolve_seed_urls(CONFIG)


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


def test_data_source_name_carries_the_chunking_config():
    # Chunking is IMMUTABLE in Bedrock, so changing it makes CloudFormation replace the data
    # source - and CloudFormation creates the replacement BEFORE deleting the original. A fixed
    # name collides inside the knowledge base and kills the deploy mid-update with
    # "DataSource with name ... already exists (409 AlreadyExists)", which is exactly what
    # happened on the 300 -> 600 token change. The name has to move when the chunking moves.
    template = _template()
    (source,) = template.find_resources("AWS::Bedrock::DataSource").values()
    name = source["Properties"]["Name"]
    chunking = CONFIG["chunking"]

    assert str(chunking["max_tokens"]) in name, name
    assert str(chunking["overlap_percentage"]) in name, name
    assert chunking["strategy"].lower().replace("_", "") in name, name
    # Bedrock's own constraint on the field: alphanumerics separated by single _ or -.
    assert re.fullmatch(r"([0-9a-zA-Z][_-]?){1,100}", name), name


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
    # With config.yaml as shipped - feedback enabled but with no destination address, so no
    # /feedback route exists (see the feedback section below for both states).
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
    # Pre-launch hardening: the browser origin allowlist is driven from config.yaml, and a
    # wildcard would let any page on the internet drive this billable endpoint (Bedrock +
    # Primo) from its visitors' browsers.
    #
    # These assert the INVARIANTS rather than a snapshot of today's list. A literal copy of the
    # list only pins the entries that happen to be in config.yaml at the moment it was written,
    # so it fails on every legitimate addition while catching none of the ways the allowlist
    # actually breaks. What follows pins those ways instead, and keeps working at any length.
    config_origins = resolve_cors_allow_origins(CONFIG)
    assert "*" not in config_origins

    # 1. The production library site is load-bearing: the widget is embedded on
    #    https://www.gavilan.edu/library/, and losing this entry takes the real product down.
    #    (Host-only Origin - the path is irrelevant to CORS - so no /library/ here.)
    assert "https://www.gavilan.edu" in config_origins, config_origins

    # 2. Every entry is a bare origin: scheme + host + optional port, with no trailing slash,
    #    no path, no query and no fragment. API Gateway matches the FULL origin string exactly,
    #    so a stray slash does not "mostly work" - it silently matches nothing, and the failure
    #    looks like a backend outage from the browser rather than a typo in config.
    for origin in config_origins:
        parsed = urlparse(origin)
        assert parsed.scheme in ("http", "https"), origin
        assert parsed.netloc, origin
        assert (parsed.path, parsed.query, parsed.fragment) == ("", "", ""), origin
        assert origin == f"{parsed.scheme}://{parsed.netloc}", origin

    # 3. That resolved list is what actually reaches the synthesized API - the stack passes
    #    config through rather than hardcoding, dropping or reordering any of it. With the demo
    #    site enabled the template also carries the demo distribution's own origin, which is a
    #    deploy-time GetAtt (a dict), not a literal - see test_demo_site_origin_is_allowlisted.
    template = _template()
    api = _one(template, "AWS::ApiGatewayV2::Api")
    cors = api["Properties"]["CorsConfiguration"]
    literal_origins = [o for o in cors["AllowOrigins"] if isinstance(o, str)]
    assert literal_origins == config_origins, cors
    assert "*" not in cors["AllowOrigins"], cors
    # Methods must cover the real routes (POST /query, GET /warm) plus the OPTIONS preflight
    # the gateway answers itself. Authorization is allowed through because /query is gated: the
    # widget sets that header from JavaScript, which makes every /query preflighted, and leaving
    # it out kills the request at the OPTIONS with a CORS error instead of a 401.
    assert set(cors["AllowMethods"]) == {"GET", "POST", "OPTIONS"}, cors
    assert cors["AllowHeaders"] == ["Content-Type", "Authorization"], cors
    # Still no cookies and no browser-managed HTTP auth, so credentialed CORS stays off - that
    # flag is not what an explicitly-set Authorization header needs.
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


def test_tier_config_rejects_the_ways_it_can_be_silently_wrong():
    # Every failure below deploys happily and only shows up as stale content weeks later, so each
    # one is a synth-time error instead.
    missing = copy.deepcopy(CONFIG)
    del missing["scraper"]["tiers"]
    with pytest.raises(ValueError, match=r"missing scraper.tiers"):
        resolve_scraper_tiers(missing)

    # A tier with no cron is a Lambda nothing ever invokes.
    no_cron = copy.deepcopy(CONFIG)
    del no_cron["scraper"]["tiers"]["fast"]["schedule_cron"]
    with pytest.raises(ValueError, match=r"missing schedule_cron"):
        resolve_scraper_tiers(no_cron)

    # A tier with no URLs is a schedule that scrapes nothing.
    no_urls = copy.deepcopy(CONFIG)
    no_urls["scraper"]["tiers"]["fast"]["urls"] = []
    with pytest.raises(ValueError, match=r"non-empty list"):
        resolve_scraper_tiers(no_urls)

    # A URL in two tiers makes "which tier owns this page" unanswerable and double-fetches it.
    duplicated = copy.deepcopy(CONFIG)
    duplicated["scraper"]["tiers"]["full"]["urls"].append(
        duplicated["scraper"]["tiers"]["fast"]["urls"][0]
    )
    with pytest.raises(ValueError, match=r"exactly one tier"):
        resolve_scraper_tiers(duplicated)

    # No full tier means no complete sweep, and the prune has nothing safe to key off.
    no_full = copy.deepcopy(CONFIG)
    del no_full["scraper"]["tiers"]["full"]
    with pytest.raises(ValueError, match=r"complete sweep"):
        resolve_scraper_tiers(no_full)


def test_a_kb_exclusion_for_an_unscraped_url_fails_synth():
    # An exclusion means "fetch this but do not index it". Naming a URL no tier fetches is a
    # no-op that reads like a working exclusion - and for databases.php it would freeze the
    # database catalog at its last-good copy with nothing in the logs to say why.
    config = copy.deepcopy(CONFIG)
    config["scraper"]["kb_exclude_urls"] = ["https://www.gavilan.edu/library/not-seeded.php"]
    app = core.App(context={"aws:cdk:bundling-stacks": []})
    with pytest.raises(ValueError, match=r"no tier scrapes"):
        GavilanChatbotStack(app, "GavilanChatbotStack", config=config)


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
    # Four S3 buckets now (widget + KB source + catalog + demo site), all fully private: public
    # access blocked and ACLs disabled via bucket-owner-enforced ownership (which OAC requires).
    # Both public-facing buckets are read ONLY through their CloudFront OAC, never directly.
    template.resource_count_is("AWS::S3::Bucket", 4)
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
    # KB source bucket + widget bucket + catalog bucket (Phase 2b) + demo site bucket.
    assert len(buckets) == 4, list(buckets)
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
    # The WHOLE tier map comes from config as a JSON string - each EventBridge rule names a tier
    # and the Lambda looks it up here. Handing over every tier (not just one) is also what lets
    # the stale-object prune key off the full corpus rather than one run's slice.
    assert json.loads(env["SCRAPER_TIERS"]) == SCRAPER_TIERS
    # bucket/KB/data-source are resource refs.
    assert "SOURCE_BUCKET" in env and "KNOWLEDGE_BASE_ID" in env and "DATA_SOURCE_ID" in env


def test_every_configured_seed_url_reaches_the_lambda_exactly_once():
    # Tier membership is the only declaration of which pages exist. A URL that fell out of the
    # env entirely would simply stop being refreshed, and the prune would then delete it from the
    # knowledge base - silently, and only on whichever run noticed.
    env = _scraper_function(_template())["Properties"]["Environment"]["Variables"]
    wired = [u for tier in json.loads(env["SCRAPER_TIERS"]).values() for u in tier["urls"]]
    assert sorted(wired) == sorted(SEED_URLS)
    assert len(wired) == len(set(wired)), "a seed URL is declared in more than one tier"


def test_each_tier_carries_its_own_cadence():
    # Cadence is per-tier and comes from config; nothing about it is spelled out in the stack.
    env = _scraper_function(_template())["Properties"]["Environment"]["Variables"]
    tiers = json.loads(env["SCRAPER_TIERS"])
    assert set(tiers) == set(SCRAPER_TIERS)
    for name, tier in tiers.items():
        assert tier["schedule_cron"] == SCRAPER_TIERS[name]["schedule_cron"]


def test_the_fast_tier_is_a_strict_subset_and_excludes_the_databases_page():
    # The two properties that make a daily run cheap: it is a handful of pages, and it cannot
    # reach databases.php, which is the only page whose scrape can cost a model call.
    fast = set(SCRAPER_TIERS["fast"]["urls"])
    assert fast, "the fast tier must declare at least one URL"
    assert fast < set(SEED_URLS)
    assert not any("databases.php" in url for url in fast)


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

    # READ the source objects. Change gating HEADs each markdown object to read back the
    # content fingerprint it stamped last time, and HeadObject is authorized as s3:GetObject.
    # Without this every page reads as changed and the gating quietly stops gating anything.
    reads = [
        s
        for s in statements
        if s["Action"] == "s3:GetObject" and "KnowledgeBaseSourceBucket" in json.dumps(s["Resource"])
    ]
    assert len(reads) == 1, reads
    assert "/*" in json.dumps(reads[0]["Resource"]), "object-level, not bucket-level"

    # StartIngestionJob to index, ListIngestionJobs to see whether a job is already running (one
    # per data source) and when the last one started - which is how a deferred change gets found
    # again without storing anything.
    ingest = [
        s
        for s in statements
        if isinstance(s["Action"], list) and "bedrock:StartIngestionJob" in s["Action"]
    ]
    assert len(ingest) == 1, ingest
    assert set(ingest[0]["Action"]) == {"bedrock:StartIngestionJob", "bedrock:ListIngestionJobs"}
    assert "KnowledgeBase" in json.dumps(ingest[0]["Resource"])


def test_scraper_env_carries_the_kb_exclusion_list():
    # databases.php must stay in the seed list (regenerate_catalog parses its HTML) while being
    # kept out of the knowledge base. If these two ever disagree, either the catalog freezes or
    # the largest redundant document in the corpus comes back.
    env = _scraper_function(_template())["Properties"]["Environment"]["Variables"]
    excluded = json.loads(env["KB_EXCLUDE_URLS"])
    assert excluded == CONFIG["scraper"].get("kb_exclude_urls", [])
    for url in excluded:
        assert url in SEED_URLS, url


def _scraper_rules(template):
    """The scheduled re-scrape rules, keyed by the tier each one invokes."""
    rules = {}
    for rule in template.find_resources("AWS::Events::Rule").values():
        targets = rule["Properties"]["Targets"]
        assert len(targets) == 1, targets
        assert "ScraperFunction" in json.dumps(targets[0]["Arn"])
        rules[json.loads(targets[0]["Input"])["tier"]] = rule
    return rules


def test_one_eventbridge_schedule_per_tier_targets_the_scraper_lambda():
    # One rule per tier, each firing on that tier's own cron and telling the Lambda which tier it
    # is. Both come straight from config, so a cadence change or a new tier is a config edit.
    template = _template()
    template.resource_count_is("AWS::Events::Rule", len(SCRAPER_TIERS))
    rules = _scraper_rules(template)

    assert set(rules) == set(SCRAPER_TIERS)
    for tier_name, tier in SCRAPER_TIERS.items():
        props = rules[tier_name]["Properties"]
        assert props["ScheduleExpression"] == tier["schedule_cron"], tier_name
        assert props["State"] == "ENABLED", tier_name


def test_the_two_tiers_do_not_share_a_firing_time():
    # Overlap is handled (the scraper defers behind a running ingestion job and the change is
    # picked up next run), but the cheapest way to not need that path is to not schedule both
    # tiers at the same minute. This catches an edit that accidentally aligns them.
    crons = [tier["schedule_cron"] for tier in SCRAPER_TIERS.values()]
    minutes_and_hours = [tuple(c.removeprefix("cron(").split()[:2]) for c in crons]
    assert len(set(minutes_and_hours)) == len(crons), crons


def test_the_fast_schedule_runs_more_often_than_the_full_one():
    # The whole point of the split. Day-of-month is '*' for a daily tier and an explicit list for
    # the slower sweep; if that inverts, "hours within a day" stops being true.
    fast_dom = SCRAPER_TIERS["fast"]["schedule_cron"].removeprefix("cron(").split()[2]
    full_dom = SCRAPER_TIERS["full"]["schedule_cron"].removeprefix("cron(").split()[2]
    assert fast_dom == "*", SCRAPER_TIERS["fast"]["schedule_cron"]
    assert full_dom != "*", SCRAPER_TIERS["full"]["schedule_cron"]


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


def _distribution(template, root_object):
    """The one CloudFront distribution serving `root_object` (widget.js vs the demo page)."""
    dists = [
        d
        for d in template.find_resources("AWS::CloudFront::Distribution").values()
        if d["Properties"]["DistributionConfig"].get("DefaultRootObject") == root_object
    ]
    assert len(dists) == 1, dists
    return dists[0]


def test_widget_distribution_uses_oac_not_oai():
    template = _template()
    # Two distributions: the production widget CDN and the demo site's own (see the demo tests).
    template.resource_count_is("AWS::CloudFront::Distribution", 2)
    # OAC is the required mechanism: an OriginAccessControl resource per distribution, and
    # NO legacy Origin Access Identity anywhere.
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 2)
    template.resource_count_is("AWS::CloudFront::CloudFrontOriginAccessIdentity", 0)

    for dist in template.find_resources("AWS::CloudFront::Distribution").values():
        origin = dist["Properties"]["DistributionConfig"]["Origins"][0]
        # The origin references the OAC and does NOT use an OAI (empty OriginAccessIdentity).
        assert "OriginAccessControlId" in origin, origin
        assert origin.get("S3OriginConfig", {}).get("OriginAccessIdentity", "") == ""

    # The production widget CDN still serves widget.js from the root.
    widget_dist = _distribution(template, "widget.js")
    assert "OriginAccessControlId" in widget_dist["Properties"]["DistributionConfig"]["Origins"][0]


def test_widget_bucket_deployment_uploads_the_widget():
    template = _template()
    # Two BucketDeployments, each rendering as a CDK bucket-deployment custom resource: the
    # widget's and the demo site's. They MUST target different buckets - a BucketDeployment
    # prunes (`aws s3 sync --delete`), so two of them sharing one bucket would delete each
    # other's objects on deploy.
    deployments = template.find_resources("Custom::CDKBucketDeployment")
    assert len(deployments) == 2, list(deployments)
    destinations = [d["Properties"]["DestinationBucketName"] for d in deployments.values()]
    assert len(destinations) == len(
        {json.dumps(d, sort_keys=True) for d in destinations}
    ), destinations
    # ...and neither of them was given a key prefix that would leave prune unscoped in a
    # shared bucket - the separation is by bucket, which cannot be misconfigured away.
    blob = json.dumps(destinations)
    assert "WidgetBucket" in blob and "DemoSiteBucket" in blob, blob


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


# --- Demo site: its own bucket + CDN, deploy-stamped page, noindex ---------------


def _demo_deployment(template):
    """The demo site's BucketDeployment custom resource (not the widget's)."""
    deps = {
        lid: r
        for lid, r in template.find_resources("Custom::CDKBucketDeployment").items()
        if "DemoSite" in lid
    }
    assert len(deps) == 1, list(deps)
    return next(iter(deps.values()))


def _config_without_demo():
    off = copy.deepcopy(CONFIG)
    off["demo_site"] = {"enabled": False}
    return off


def _template_from(config):
    app = core.App(context={"aws:cdk:bundling-stacks": []})
    stack = GavilanChatbotStack(app, "GavilanChatbotStack", config=config)
    return assertions.Template.from_stack(stack)


def test_demo_site_has_its_own_private_bucket_and_distribution():
    # The demo gets a SEPARATE bucket and distribution from the widget on purpose: a
    # BucketDeployment prunes with `aws s3 sync --delete`, so sharing the widget bucket would
    # put production widget.js one misconfigured prefix away from deletion. Separate buckets
    # make the interference impossible rather than merely configured against.
    template = _template()
    demo = _distribution(template, "index.html")
    cfg = demo["Properties"]["DistributionConfig"]
    # Served from a private bucket through OAC, HTTPS-only, like the widget.
    assert cfg["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https", cfg
    origin = cfg["Origins"][0]
    assert "OriginAccessControlId" in origin, origin
    assert "DemoSiteBucket" in json.dumps(origin["DomainName"]), origin
    # The shareable link is the bare domain, so CloudFront must map / to the page.
    assert cfg["DefaultRootObject"] == "index.html", cfg


def test_demo_page_is_stamped_with_the_deployed_api_and_widget_urls():
    # The whole point: a hardcoded endpoint would break a fresh install in another account.
    # Source.data stages the page with substitution markers and resolves them AT DEPLOY, so
    # every placeholder occurrence must map to a deploy-time GetAtt - never a literal.
    template = _template()
    props = _demo_deployment(template)["Properties"]
    (markers,) = props["SourceMarkers"]

    page = (
        pathlib.Path(__file__).resolve().parents[3] / "frontend" / "demo-site.html"
    ).read_text(encoding="utf-8")
    expected = sum(
        page.count(token)
        for token in ("__API_URL__", "__WIDGET_SRC__", "__USER_POOL_ID__", "__CLIENT_ID__")
    )
    assert expected >= 4, "the demo page must carry every deploy-time placeholder"
    assert len(markers) == expected, (len(markers), expected)

    # Each marker resolves to the HTTP API endpoint, the widget CDN domain, or one of the two
    # sign-in ids (TEMPORARY, with the gate) - four distinct deploy-time values, no literals.
    resolved = {json.dumps(v, sort_keys=True) for v in markers.values()}
    assert len(resolved) == 4, resolved
    blob = json.dumps(sorted(resolved))
    assert "ChatbotHttpApi" in blob and "ApiEndpoint" in blob, blob
    assert "WidgetDistribution" in blob and "DomainName" in blob, blob
    assert "DemoAuthUserPool" in blob, blob
    assert "DemoAuthClient" in blob, blob

    # It lands in the demo bucket, and the deploy invalidates the page so a redeploy is
    # visible immediately instead of served from the edge cache.
    assert "DemoSiteBucket" in json.dumps(props["DestinationBucketName"])
    assert props["DistributionPaths"] == ["/*"], props
    assert "DemoSiteDistribution" in json.dumps(props["DistributionId"])


def test_demo_page_missing_a_placeholder_fails_synth():
    # A renamed placeholder must break the build, not ship a page whose widget tag points at
    # the literal string "__API_URL__" and silently never reaches the backend. Pointed at a
    # real frontend file that carries no placeholders (the offline mock harness).
    import infra.infra_stack as stack_module

    original = stack_module._DEMO_PAGE_FILE
    stack_module._DEMO_PAGE_FILE = "demo.html"
    try:
        with pytest.raises(ValueError, match=r"deploy-time placeholder"):
            _template_from(CONFIG)
    finally:
        stack_module._DEMO_PAGE_FILE = original


def test_demo_site_origin_is_allowlisted_at_deploy_time():
    # The demo runs the REAL cross-origin embed, so the browser needs its origin on the API's
    # CORS allowlist or every /query and /warm call from the page is blocked. It must be a
    # deploy-time GetAtt on the demo distribution, not a hardcoded CloudFront domain.
    template = _template()
    (api,) = template.find_resources("AWS::ApiGatewayV2::Api").values()
    origins = api["Properties"]["CorsConfiguration"]["AllowOrigins"]
    tokens = [o for o in origins if not isinstance(o, str)]
    assert len(tokens) == 1, origins
    parts = tokens[0]["Fn::Join"][1]
    assert parts[0] == "https://", parts
    assert parts[1]["Fn::GetAtt"][1] == "DomainName", parts
    assert "DemoSiteDistribution" in parts[1]["Fn::GetAtt"][0], parts
    # Still an explicit allowlist, never a wildcard.
    assert "*" not in origins, origins


def test_demo_site_is_marked_as_a_demo_and_noindex():
    template = _template()
    # noindex at the edge: a crawler that never parses the HTML still gets the header, and the
    # header cannot be lost to an edit of the page.
    policies = template.find_resources("AWS::CloudFront::ResponseHeadersPolicy")
    assert len(policies) == 1, list(policies)
    (policy,) = policies.values()
    headers = policy["Properties"]["ResponseHeadersPolicyConfig"]["CustomHeadersConfig"]["Items"]
    robots = [h for h in headers if h["Header"] == "X-Robots-Tag"]
    assert robots and "noindex" in robots[0]["Value"], headers
    # ...and it is attached to the DEMO distribution only; the production widget CDN keeps
    # its original behavior with no response-headers policy.
    demo_behavior = _distribution(template, "index.html")["Properties"]["DistributionConfig"][
        "DefaultCacheBehavior"
    ]
    assert "ResponseHeadersPolicyId" in demo_behavior, demo_behavior
    widget_behavior = _distribution(template, "widget.js")["Properties"]["DistributionConfig"][
        "DefaultCacheBehavior"
    ]
    assert "ResponseHeadersPolicyId" not in widget_behavior, widget_behavior

    # The page itself says so in the markup and to a human reader.
    page = (
        pathlib.Path(__file__).resolve().parents[3] / "frontend" / "demo-site.html"
    ).read_text(encoding="utf-8")
    assert re.search(r'<meta\s+name="robots"\s+content="[^"]*noindex', page), "missing noindex"
    assert "demo-banner" in page, "the page must carry a visible demo banner"
    assert "Demo site" in page, "the banner must label the page as a demo"
    assert "the Gavilan College Library website" in page, "the banner must disclaim the real site"


def test_demo_page_embeds_the_production_widget_rather_than_a_copy():
    # Requirement: the demo must exercise the SAME widget by the SAME delivery path, or it
    # will drift from what the library actually embeds. So: exactly one script tag, pointing
    # at the deploy-stamped CDN URL, and no mock and no inlined widget code anywhere.
    page = (
        pathlib.Path(__file__).resolve().parents[3] / "frontend" / "demo-site.html"
    ).read_text(encoding="utf-8")
    # EXACTLY ONE script with a src: the widget, from the deploy-stamped CDN URL. That is the
    # invariant - one external dependency, fetched over the production delivery path.
    external = re.findall(r"<script\b[^>]*\bsrc\s*=[^>]*>", page)
    assert len(external) == 1, external
    assert 'src="__WIDGET_SRC__"' in external[0], external
    assert 'data-api-url="__API_URL__"' in external[0], external
    # The dev-only offline mock must never be loaded here (demo.html is where that lives).
    assert not re.search(r'src\s*=\s*"[^"]*mock\.js"', page), "the demo must not load the mock"

    # Inline script is allowed for the page's OWN behavior (the cost panel), but it must not
    # be a second copy of the widget. The original single-script rule was a proxy for that;
    # this asserts the thing it was protecting directly, so the demo cannot grow a forked
    # renderer, a canned-answer table, or its own call to /query.
    inline = "\n".join(re.findall(r"<script(?![^>]*\bsrc\b)[^>]*>(.*?)</script>", page, re.S))
    for forbidden in ("mount(", "normalizeResponse", "attachShadow", "trigger error"):
        assert forbidden not in inline, f"inline script must not reimplement the widget: {forbidden}"
    # The page must never talk to the backend itself; only the widget does.
    assert "fetch(" not in inline, "the demo page must not call the API directly"
    assert "__API_URL__" not in inline, "only the widget tag carries the endpoint"


def test_demo_site_can_be_turned_off_in_config():
    # Turning the knob off must take the whole demo with it - bucket, CDN, page, CORS origin
    # and output - and leave the production widget path exactly as it was.
    template = _template_from(_config_without_demo())
    template.resource_count_is("AWS::S3::Bucket", 3)
    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
    template.resource_count_is("Custom::CDKBucketDeployment", 1)
    template.resource_count_is("AWS::CloudFront::ResponseHeadersPolicy", 0)
    assert "DemoSiteUrl" not in template.find_outputs("*")

    # The allowlist falls back to exactly what config lists and nothing else: no leftover demo
    # entry, and no deploy-time token (every element is a plain string, not an Fn::GetAtt).
    # Asserted against config rather than a copied-out list so adding a real origin does not
    # fail a test that is about the demo knob.
    (api,) = template.find_resources("AWS::ApiGatewayV2::Api").values()
    assert (
        api["Properties"]["CorsConfiguration"]["AllowOrigins"]
        == resolve_cors_allow_origins(CONFIG)
    )
    # The widget still ships from its own bucket at the same root object.
    assert _distribution(template, "widget.js")


def test_demo_site_does_not_change_widget_delivery():
    # The main regression risk. Compare the production widget path with the demo enabled and
    # disabled: the distribution config, the bucket deployment's target/prune/invalidation,
    # and the paste-ready embed tag must be identical either way.
    with_demo = _template()
    without_demo = _template_from(_config_without_demo())

    assert (
        _distribution(with_demo, "widget.js")["Properties"]["DistributionConfig"]
        == _distribution(without_demo, "widget.js")["Properties"]["DistributionConfig"]
    )

    def widget_deployment(template):
        deps = {
            lid: r
            for lid, r in template.find_resources("Custom::CDKBucketDeployment").items()
            if "DemoSite" not in lid
        }
        assert len(deps) == 1, list(deps)
        return next(iter(deps.values()))["Properties"]

    a, b = widget_deployment(with_demo), widget_deployment(without_demo)
    for key in ("DestinationBucketName", "DistributionPaths", "Prune", "SourceObjectKeys"):
        assert a.get(key) == b.get(key), (key, a.get(key), b.get(key))
    # Still pruning its own bucket, still only widget.js, still invalidated on deploy.
    assert a["Prune"] is True
    assert a["DistributionPaths"] == ["/widget.js"]
    assert "SourceMarkers" not in a, "the widget upload must stay a plain asset copy"

    assert (
        with_demo.find_outputs("WidgetEmbedTag") == without_demo.find_outputs("WidgetEmbedTag")
    )


# --- Bedrock Guardrail: ONE input screen, PROMPT_ATTACK only --------------------
#
# The scope-down these pin: the screen runs BEFORE the system prompt, so anything it blocks
# or rewrites pre-empts the prompt's crisis handling. Everything except PROMPT_ATTACK came
# out, and the output guardrail was deleted rather than disabled.


def _the_guardrail(template):
    """The single AWS::Bedrock::Guardrail in the stack. Asserts there is exactly one, so a
    reintroduced output guardrail fails here first."""
    resources = list(template.find_resources("AWS::Bedrock::Guardrail").values())
    assert len(resources) == 1, [r["Properties"].get("Name") for r in resources]
    return resources[0]


def _filters(guardrail):
    return {
        f["Type"]: f
        for f in guardrail["Properties"]["ContentPolicyConfig"]["FiltersConfig"]
    }


def _version_descriptions(template):
    versions = template.find_resources("AWS::Bedrock::GuardrailVersion")
    return sorted(v["Properties"]["Description"] for v in versions.values())


def test_exactly_one_guardrail_and_it_is_the_input_screen():
    template = _template()
    gr = CONFIG["guardrail"]
    # ONE guardrail resource. The output backstop is deleted, not disabled: a second
    # AWS::Bedrock::Guardrail anywhere in this stack is the regression.
    template.resource_count_is("AWS::Bedrock::Guardrail", 1)
    assert _the_guardrail(template)["Properties"]["Name"] == gr["name"]


def test_guardrail_screens_prompt_attack_and_nothing_else():
    template = _template()
    gr = CONFIG["guardrail"]
    guardrail = _the_guardrail(template)
    assert guardrail["Properties"]["BlockedInputMessaging"] == gr["blocked_input_messaging"]

    filters = _filters(guardrail)
    # EXACTLY one content filter, and it is PROMPT_ATTACK. HATE / SEXUAL / INSULTS / VIOLENCE
    # / MISCONDUCT are gone on purpose - each of them would answer a student in crisis with
    # canned decline copy before the system prompt ever saw the message.
    assert set(filters) == {"PROMPT_ATTACK"}
    assert filters["PROMPT_ATTACK"]["InputStrength"] == "HIGH"
    # Input-only: AWS requires NONE here, and nothing applies this guardrail to output anyway.
    assert filters["PROMPT_ATTACK"]["OutputStrength"] == "NONE"


def test_guardrail_has_no_pii_policy():
    # No sensitive-information policy anywhere: anonymization would hand the model {NAME} and
    # {ADDRESS} in place of the details that make an urgent message legible.
    template = _template()
    props = _the_guardrail(template)["Properties"]
    assert "SensitiveInformationPolicyConfig" not in props
    # ...and the config carries no entity list for one to be rebuilt from.
    assert "pii_anonymize_entities" not in CONFIG["guardrail"]


def test_one_guardrail_version_wired_to_query_lambda_with_no_output_env():
    template = _template()
    template.resource_count_is("AWS::Bedrock::GuardrailVersion", 1)
    (fn,) = [
        f
        for f in template.find_resources("AWS::Lambda::Function").values()
        if f["Properties"].get("Handler") == "handler.lambda_handler"
    ]
    env = fn["Properties"]["Environment"]["Variables"]
    # The input screen is wired...
    assert "INPUT_GUARDRAIL_ID" in env and "INPUT_GUARDRAIL_VERSION" in env
    # ...and nothing else guardrail-shaped is. GUARDRAIL_TRACE configured the Converse-attached
    # guardrail's trace; with nothing attached there is no trace to ask for.
    assert "OUTPUT_GUARDRAIL_ID" not in env
    assert "OUTPUT_GUARDRAIL_VERSION" not in env
    assert "GUARDRAIL_TRACE" not in env


def test_guardrail_descriptions_fit_the_cloudformation_cap():
    # AWS::Bedrock::Guardrail and ::GuardrailVersion both cap Description at 200 characters,
    # and the L1 Cfn constructs do NOT enforce it - `cdk synth` is happy and the change set is
    # rejected at deploy ("expected maxLength: 200"). Prose here is a deploy-time failure, so
    # it gets pinned at synth instead: rationale belongs in code comments, not in this field.
    template = _template()
    descriptions = [
        (logical_id, res["Properties"]["Description"])
        for kind in ("AWS::Bedrock::Guardrail", "AWS::Bedrock::GuardrailVersion")
        for logical_id, res in template.find_resources(kind).items()
        if "Description" in res["Properties"]
    ]
    assert descriptions, "expected the guardrail resources to carry descriptions"
    too_long = {lid: len(d) for lid, d in descriptions if len(d) > 200}
    assert not too_long, too_long


def test_guardrail_version_description_is_a_config_content_hash():
    # The version description must be a content hash of the resolved guardrail config, not a
    # fixed literal, so any config change publishes a new immutable version.
    template = _template()
    descs = _version_descriptions(template)
    assert len(descs) == 1
    assert re.match(r"^input config-[0-9a-f]{12}$", descs[0]), descs[0]


def test_guardrail_config_change_forces_new_version_description():
    # Editing the guardrail config changes the version description (the ONLY property that
    # changes), which is what forces CloudFormation to publish a new numbered version rather
    # than silently no-op'ing and leaving the Lambda on the stale one.
    base_descs = _version_descriptions(_template())

    mutated = copy.deepcopy(CONFIG)
    # Flip the one filter strength; nothing else.
    mutated["guardrail"]["content_filters"][0]["input_strength"] = "LOW"
    app = core.App()
    stack = GavilanChatbotStack(app, "MutatedGuardrail", config=mutated)
    mutated_descs = _version_descriptions(assertions.Template.from_stack(stack))

    assert base_descs != mutated_descs


def test_query_lambda_role_can_apply_exactly_one_guardrail():
    template = _template()
    # The query role is granted bedrock:ApplyGuardrail for the standalone input screen, scoped
    # to that ONE guardrail ARN. A second ARN here would mean an output guardrail came back.
    apply_stmts = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            if stmt.get("Action") == "bedrock:ApplyGuardrail":
                apply_stmts.append(stmt)
    assert len(apply_stmts) == 1, apply_stmts
    resource = apply_stmts[0]["Resource"]
    # One ARN (a GetAtt token to the guardrail's ARN attribute), not a list of two.
    assert not isinstance(resource, list), resource


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


# --- demo-site cost model (rates + measured usage, stamped from config) ---------------------


def _staged_demo_page(tmp_path):
    """The demo page AS STAGED FOR DEPLOYMENT: synth to a temp cloud assembly and read the
    index.html that Source.data wrote. This is the only place the substituted content is
    observable - the CloudFormation template carries markers, not the file body."""
    app = core.App(
        outdir=str(tmp_path), context={"aws:cdk:bundling-stacks": []}
    )
    GavilanChatbotStack(app, "GavilanChatbotStack", config=CONFIG)
    app.synth()
    pages = [p for p in pathlib.Path(tmp_path).rglob("index.html")]
    assert pages, "no staged demo page found in the cloud assembly"
    # One demo page; if a second ever appears the assertion below would be ambiguous.
    assert len(pages) == 1, pages
    return pages[0].read_text(encoding="utf-8")


def test_cost_model_is_stamped_into_the_page_at_synth_not_hardcoded(tmp_path):
    # The page must never carry rates or measured constants of its own: config.yaml is the
    # single source of truth, exactly as it is for every other knob. This asserts the JSON
    # actually lands in the STAGED page and that the raw placeholder is gone.
    staged = _staged_demo_page(tmp_path)
    assert "__COST_MODEL__" not in staged, "the cost-model placeholder must be substituted"
    # Values that can only have come from config.yaml's cost_model block.
    rates = CONFIG["cost_model"]["rates"]
    assert '"generation_input_per_1m": %s' % rates["generation_input_per_1m"] in staged
    assert '"context_tokens_per_call_base"' in staged
    assert '"sample_questions": %d' % CONFIG["cost_model"]["measured"]["sample_questions"] in staged
    # The URL placeholders stay as deploy-time markers at this stage (resolved by the
    # deployment custom resource, not by synth), so they must still be unresolved here.
    assert "<<marker:" in staged, "URL placeholders must remain deploy-time markers"


def test_demo_page_carries_no_hardcoded_rates_or_measured_constants():
    # The page's own source must contain the placeholder and nothing that looks like a rate.
    page = (
        pathlib.Path(__file__).resolve().parents[3] / "frontend" / "demo-site.html"
    ).read_text(encoding="utf-8")
    assert "__COST_MODEL__" in page, "the page must read its cost model from the placeholder"
    # The measured constants and rates live in config; none of them may be spelled here.
    for leaked in ("10666", "generation_input_per_1m: 3", "3.00 / 1M", "$0.15 /"):
        assert leaked not in page, f"rate/constant leaked into the page: {leaked}"


def test_every_cost_model_key_the_page_reads_still_exists_in_config():
    # The page reads its numbers off the stamped JSON as R.<rate> / Q.<measured> / B.<input>.
    # A key that config no longer carries reads as `undefined`, and the arithmetic silently
    # produces NaN rather than failing - the demo would show "$NaN" to the client, or worse,
    # quietly drop a line item. This caught the guardrail scope-down: removing the PII rate
    # and the PII units figure had to remove their uses on the page in the same commit.
    page = (
        pathlib.Path(__file__).resolve().parents[3] / "frontend" / "demo-site.html"
    ).read_text(encoding="utf-8")
    cost_model = CONFIG["cost_model"]
    for prefix, block in (("R", "rates"), ("Q", "measured"), ("B", "baseline")):
        used = set(re.findall(rf"\b{prefix}\.([a-z_0-9]+)", page))
        missing = used - set(cost_model[block])
        assert not missing, f"{block}: page reads {sorted(missing)}, config has no such key"


def test_cost_panel_is_hidden_by_default_and_grouped_with_the_demo_banner():
    # Requirement: the default experience says nothing about money anywhere, and the one
    # control reads as demo scaffolding rather than something the college would ship.
    page = (
        pathlib.Path(__file__).resolve().parents[3] / "frontend" / "demo-site.html"
    ).read_text(encoding="utf-8")
    # The panel ships hidden.
    assert re.search(r'<section class="costpanel" id="cost-panel" hidden', page), page[:200]
    # The toggle lives INSIDE the demo banner, not in the library masthead/nav/footer.
    banner = page.split('<div class="demo-banner"', 1)[1].split("</div>\n  </div>", 1)[0]
    assert 'id="cost-toggle"' in banner, "the cost control must sit in the demo banner"
    # ...and nowhere else.
    assert page.count('id="cost-toggle"') == 1

    # No cost wording in the library chrome: masthead, nav, search band, cards, footer.
    library_chrome = page.split("</section>", 1)[1] if "</section>" in page else page
    for chrome_tag in ("<header class=\"masthead\"", "<nav class=\"nav\"", "<footer>"):
        assert chrome_tag in page
    # The words only appear inside the demo banner + cost panel, never in the sample content.
    main = page.split("<main>", 1)[1].split("</main>", 1)[0]
    for word in ("cost", "$", "price", "pricing", "billing"):
        assert word.lower() not in main.lower(), f"library content must not mention {word!r}"


def test_demo_embed_opts_into_usage_events_and_production_embed_does_not():
    # The demo page opts in explicitly; the tag the CDK output hands the library does not,
    # so a production embed never asks for the debug payload.
    page = (
        pathlib.Path(__file__).resolve().parents[3] / "frontend" / "demo-site.html"
    ).read_text(encoding="utf-8")
    assert 'data-usage-events="true"' in page

    template = _template()
    embed = template.find_outputs("WidgetEmbedTag")["WidgetEmbedTag"]["Value"]
    assert "data-usage-events" not in json.dumps(embed), embed


def test_cost_model_reaches_the_page_for_a_fresh_install_in_another_account():
    # The cost model is synth-time config, so unlike the URLs it must NOT become a marker.
    # Markers still number exactly the URL placeholders; a regression that turned the JSON
    # into a marker would break deployment substitution.
    template = _template()
    props = _demo_deployment(template)["Properties"]
    (markers,) = props["SourceMarkers"]
    page = (
        pathlib.Path(__file__).resolve().parents[3] / "frontend" / "demo-site.html"
    ).read_text(encoding="utf-8")
    assert len(markers) == sum(
        page.count(token)
        for token in ("__API_URL__", "__WIDGET_SRC__", "__USER_POOL_ID__", "__CLIENT_ID__")
    )


# --- Feedback endpoint: SNS topic + email subscription + POST /feedback ----------------------
#
# config.yaml ships `feedback.enabled: true` with an EMPTY notify_email, so the default template
# (`_template()`) has NO feedback resources at all. Tests that need the provisioned shape build
# their own config with an address, which is also the honest way to test it: the destination is a
# handoff value, not something the repo can commit.

# example.edu is reserved for documentation, so no real mailbox is referenced anywhere here.
_FEEDBACK_EMAIL = "library-reference@example.edu"


def _config_with_feedback(**overrides):
    cfg = copy.deepcopy(CONFIG)
    block = dict(cfg.get("feedback") or {})
    block.update({"enabled": True, "notify_email": _FEEDBACK_EMAIL})
    block.update(overrides)
    cfg["feedback"] = block
    return cfg


def _config_feedback_off():
    cfg = copy.deepcopy(CONFIG)
    cfg["feedback"] = {"enabled": False}
    return cfg


def _feedback_template():
    return _template_from(_config_with_feedback())


def _feedback_function(template):
    (fn,) = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Handler": "feedback_handler.lambda_handler"}},
    ).values()
    return fn


def _statements_for_role_of(template, handler_name):
    """The IAM policy statements attached to the role of the function with this handler."""
    (fn,) = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": handler_name}}
    ).values()
    role_id = fn["Properties"]["Role"]["Fn::GetAtt"][0]
    statements = []
    for pol in template.find_resources("AWS::IAM::Policy").values():
        if any(r.get("Ref") == role_id for r in pol["Properties"].get("Roles", [])):
            statements.extend(pol["Properties"]["PolicyDocument"]["Statement"])
    return statements


def test_no_feedback_endpoint_exists_when_no_address_is_configured():
    # The shipped config: enabled, but with nowhere to send. Nothing is created - no topic, no
    # subscription, no Lambda, no route - because an endpoint that accepts reports it cannot
    # deliver loses them silently, and the email is the only record there is.
    template = _template()
    template.resource_count_is("AWS::SNS::Topic", 0)
    template.resource_count_is("AWS::SNS::Subscription", 0)
    template.resource_count_is("AWS::SNS::TopicPolicy", 0)
    assert not template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Handler": "feedback_handler.lambda_handler"}},
    )
    keys = {
        r["Properties"]["RouteKey"]
        for r in template.find_resources("AWS::ApiGatewayV2::Route").values()
    }
    assert "POST /feedback" not in keys, keys
    assert "FeedbackApiUrl" not in template.find_outputs("*")

    # ...but it is NOT silent about it. This case is a mistake rather than a choice, so the deploy
    # output says why there is no endpoint instead of leaving it to be discovered by a lost report.
    status = template.find_outputs("FeedbackStatus")["FeedbackStatus"]["Value"]
    assert "notify_email" in status, status


def test_feedback_disabled_creates_nothing_and_says_nothing():
    # Deliberately off is different from misconfigured: no resources AND no status output.
    template = _template_from(_config_feedback_off())
    template.resource_count_is("AWS::SNS::Topic", 0)
    template.resource_count_is("AWS::SNS::Subscription", 0)
    outputs = template.find_outputs("*")
    assert "FeedbackApiUrl" not in outputs
    assert "FeedbackStatus" not in outputs


def test_feedback_route_topic_and_subscription_are_created_when_configured():
    template = _feedback_template()
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Route", {"RouteKey": "POST /feedback"}
    )
    # ONE topic with ONE subscription: exactly one destination, from config, over email.
    template.resource_count_is("AWS::SNS::Topic", 1)
    template.resource_count_is("AWS::SNS::Subscription", 1)
    template.has_resource_properties(
        "AWS::SNS::Subscription",
        {"Protocol": "email", "Endpoint": _FEEDBACK_EMAIL},
    )
    # The endpoint URL is handed back at deploy, with the confirmation step spelled out (SNS
    # delivers nothing until the recipient clicks the link it mails them).
    out = template.find_outputs("FeedbackApiUrl")["FeedbackApiUrl"]
    assert "/feedback" in json.dumps(out["Value"]), out
    assert "confirmation" in out["Description"].lower(), out


def test_feedback_email_address_is_never_hardcoded_in_the_stack():
    # The destination comes from config and only from config: a different address in config must
    # be the ONLY address in the template.
    other = "someone-else@example.edu"
    template = _template_from(_config_with_feedback(notify_email=other))
    (sub,) = template.find_resources("AWS::SNS::Subscription").values()
    assert sub["Properties"]["Endpoint"] == other
    assert _FEEDBACK_EMAIL not in json.dumps(template.to_json())


def test_feedback_lambda_is_its_own_function_with_its_own_log_group():
    template = _feedback_template()
    fn = _feedback_function(template)["Properties"]
    assert fn["Runtime"] == "python3.13"
    # One validate + one publish: no retrieval, no generation, so it needs neither the query
    # Lambda's 30s budget nor its memory.
    assert fn["Timeout"] == 10
    assert fn["MemorySize"] == 128

    log_group_ref = fn["LoggingConfig"]["LogGroup"]["Ref"]
    log_groups = template.find_resources("AWS::Logs::LogGroup")
    assert log_group_ref in log_groups, log_group_ref
    assert log_groups[log_group_ref]["Properties"]["RetentionInDays"] == 90
    assert log_groups[log_group_ref].get("DeletionPolicy") == "Delete"
    # Its OWN group, not the query Lambda's - the two paths' logs never mix.
    query_fn = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": "handler.lambda_handler"}}
    )
    query_group = next(iter(query_fn.values()))["Properties"]["LoggingConfig"]["LogGroup"]["Ref"]
    assert log_group_ref != query_group


def test_feedback_role_can_publish_to_the_topic_and_nothing_else():
    # Least privilege, and the reason this is a separate function: publishing an email needs
    # sns:Publish, while the query role can already invoke Bedrock and read the knowledge base.
    template = _feedback_template()
    statements = _statements_for_role_of(template, "feedback_handler.lambda_handler")
    assert statements, "the feedback role should carry an inline policy"

    (topic_id,) = template.find_resources("AWS::SNS::Topic").keys()
    publish = [s for s in statements if "sns:Publish" in json.dumps(s.get("Action"))]
    assert len(publish) == 1, statements
    assert publish[0]["Effect"] == "Allow"
    assert topic_id in json.dumps(publish[0]["Resource"]), publish[0]
    assert "*" != publish[0]["Resource"]

    # No Bedrock, no knowledge base, no S3, and no wildcard resource anywhere on this role.
    actions = json.dumps([s.get("Action") for s in statements])
    for forbidden in ("bedrock:", "s3:", "s3vectors:", "dynamodb:"):
        assert forbidden not in actions, actions
    assert '"Resource": "*"' not in json.dumps(statements)


def test_feedback_caps_are_wired_from_config_to_the_lambda():
    # Feedback text never reaches a model, so the guardrails do not screen it: these caps are the
    # controls that exist, and a cap that stops at config.yaml is not a control.
    cfg = _config_with_feedback(max_comment_chars=250, max_body_bytes=4096, max_sources=5)
    template = _template_from(cfg)
    env = _feedback_function(template)["Properties"]["Environment"]["Variables"]
    assert env["FEEDBACK_MAX_COMMENT_CHARS"] == "250"
    assert env["FEEDBACK_MAX_BODY_BYTES"] == "4096"
    assert env["FEEDBACK_MAX_SOURCES"] == "5"
    # The destination reaches it as the topic ARN, never as an address.
    (topic_id,) = template.find_resources("AWS::SNS::Topic").keys()
    assert topic_id in json.dumps(env["FEEDBACK_TOPIC_ARN"])
    assert _FEEDBACK_EMAIL not in json.dumps(env)


def test_feedback_and_query_lambdas_are_not_cross_wired():
    # The feedback function has no business knowing about the KB, and the query function has no
    # business holding a topic ARN it cannot publish to.
    template = _feedback_template()
    feedback_env = _feedback_function(template)["Properties"]["Environment"]["Variables"]
    (query_fn,) = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": "handler.lambda_handler"}}
    ).values()
    query_env = query_fn["Properties"]["Environment"]["Variables"]

    for key in ("KNOWLEDGE_BASE_ID", "GENERATION_MODEL_ID", "CATALOG_BUCKET"):
        assert key not in feedback_env, key
    assert not [k for k in query_env if k.startswith("FEEDBACK_")], query_env


def test_feedback_cors_comes_from_the_shared_config_allowlist():
    # D-20260729-7: CORS is enforced at API Gateway only, from config.yaml, exact full-origin
    # match. /feedback must use the SAME mechanism as /query rather than its own: cors_preflight
    # is configured on the API, so being on that API IS the mechanism.
    template = _feedback_template()
    apis = template.find_resources("AWS::ApiGatewayV2::Api")
    assert len(apis) == 1, list(apis)
    (api_id, api) = next(iter(apis.items()))
    cors = api["Properties"]["CorsConfiguration"]

    literal_origins = [o for o in cors["AllowOrigins"] if isinstance(o, str)]
    assert literal_origins == resolve_cors_allow_origins(CONFIG), cors
    assert "*" not in cors["AllowOrigins"], cors

    # The feedback route hangs off that same API, so it inherits that allowlist. Nothing in the
    # feedback path declares an origin of its own.
    (route,) = template.find_resources(
        "AWS::ApiGatewayV2::Route", {"Properties": {"RouteKey": "POST /feedback"}}
    ).values()
    assert route["Properties"]["ApiId"] == {"Ref": api_id}, route


def test_feedback_route_inherits_the_stage_throttle():
    # Stage throttling is the volume control for the whole stage; a per-route override on
    # /feedback would quietly exempt it.
    template = _feedback_template()
    (stage,) = template.find_resources("AWS::ApiGatewayV2::Stage").values()
    props = stage["Properties"]
    assert props["DefaultRouteSettings"] == {
        "ThrottlingRateLimit": CONFIG["http_api"]["throttling_rate_limit"],
        "ThrottlingBurstLimit": CONFIG["http_api"]["throttling_burst_limit"],
    }
    assert "RouteSettings" not in props, props


def test_feedback_route_has_its_own_integration_on_the_shared_api():
    template = _feedback_template()
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    keys = {r["Properties"]["RouteKey"] for r in routes.values()}
    assert keys == {"POST /query", "GET /warm", "POST /feedback"}, keys
    # Two integrations: /query + /warm share the query Lambda's, /feedback has its own.
    integrations = template.find_resources("AWS::ApiGatewayV2::Integration")
    assert len(integrations) == 2, list(integrations)
    (feedback_route,) = template.find_resources(
        "AWS::ApiGatewayV2::Route", {"Properties": {"RouteKey": "POST /feedback"}}
    ).values()
    fn_id = list(
        template.find_resources(
            "AWS::Lambda::Function",
            {"Properties": {"Handler": "feedback_handler.lambda_handler"}},
        )
    )[0]
    target = json.dumps(feedback_route["Properties"]["Target"])
    integration_id = next(
        i for i in integrations if i in target
    )
    assert fn_id in json.dumps(integrations[integration_id]["Properties"]["IntegrationUri"])


def test_feedback_topic_denies_non_tls_publishes():
    template = _feedback_template()
    (policy,) = template.find_resources("AWS::SNS::TopicPolicy").values()
    doc = json.dumps(policy["Properties"]["PolicyDocument"])
    assert '"Deny"' in doc, doc
    assert "SecureTransport" in doc, doc


def test_feedback_introduces_no_store():
    # Hard constraint: the email IS the record. No table, no queue, no bucket, no versioned
    # object - nothing that accumulates student reports.
    template = _feedback_template()
    template.resource_count_is("AWS::DynamoDB::Table", 0)
    template.resource_count_is("AWS::SQS::Queue", 0)
    # Same bucket count as with feedback off: the feedback path adds no storage.
    assert len(template.find_resources("AWS::S3::Bucket")) == len(
        _template().find_resources("AWS::S3::Bucket")
    )


def test_feedback_does_not_change_the_query_path():
    # The regression risk of adding a route to a shared API. The query Lambda, its role and its
    # two routes must be identical whether or not feedback is provisioned.
    with_feedback = _feedback_template()
    without = _template()

    def query_fn(template):
        (fn,) = template.find_resources(
            "AWS::Lambda::Function", {"Properties": {"Handler": "handler.lambda_handler"}}
        ).values()
        return fn["Properties"]

    a, b = query_fn(with_feedback), query_fn(without)
    for key in ("Environment", "Timeout", "MemorySize", "Runtime", "Handler"):
        assert a.get(key) == b.get(key), key

    def route_props(template):
        return {
            r["Properties"]["RouteKey"]: r["Properties"]["AuthorizationType"]
            for r in template.find_resources("AWS::ApiGatewayV2::Route").values()
            if r["Properties"]["RouteKey"] != "POST /feedback"
        }

    assert route_props(with_feedback) == route_props(without)
    assert _query_role_statements(with_feedback) == _query_role_statements(without)
    # ...and the paste-ready embed tag the library uses is untouched.
    assert with_feedback.find_outputs("WidgetEmbedTag") == without.find_outputs(
        "WidgetEmbedTag"
    )


# --- Feedback config validation (synth-time) --------------------------------------------------


def test_feedback_config_resolves_the_three_states():
    off = resolve_feedback(_config_feedback_off())
    assert off["provision"] is False and off["status"] is None

    # Enabled with no address: nothing provisioned, but a reason to show at deploy.
    unconfigured = resolve_feedback(CONFIG)
    assert unconfigured["provision"] is False
    assert "notify_email" in unconfigured["status"]

    on = resolve_feedback(_config_with_feedback())
    assert on["provision"] is True
    assert on["email"] == _FEEDBACK_EMAIL

    # An absent block behaves as off, so a config predating this feature still synths.
    assert resolve_feedback({})["provision"] is False


def test_feedback_config_rejects_a_malformed_address():
    # A typo must be loud: SNS would happily create a subscription that can never be confirmed,
    # and every report after that is lost with no error anywhere.
    for bad in (
        "librarian",
        "librarian@",
        "@example.edu",
        "librarian@example",
        "librarian@example .edu",
        "Librarian <librarian@example.edu>",
        "a@example.edu, b@example.edu",
        "x" * 250 + "@example.edu",
    ):
        with pytest.raises(ValueError, match=r"not a valid email address"):
            resolve_feedback(_config_with_feedback(notify_email=bad))

    with pytest.raises(ValueError, match=r"must be an email address string"):
        resolve_feedback(_config_with_feedback(notify_email=42))


def test_feedback_config_rejects_a_broken_cap():
    # A cap of zero (or a string, or a bool) removes a control that exists precisely because
    # feedback text is never screened by a guardrail. Never silently defaulted.
    for key in ("max_comment_chars", "max_body_bytes", "max_sources"):
        for bad in (0, -1, "1000", 12.5, True, None):
            with pytest.raises(ValueError, match=rf"feedback\.{key}"):
                resolve_feedback(_config_with_feedback(**{key: bad}))


def test_feedback_caps_are_validated_even_while_disabled():
    # Turning the feature on later must not be the moment a config error first appears.
    cfg = copy.deepcopy(CONFIG)
    cfg["feedback"] = {"enabled": False, "max_comment_chars": 0}
    with pytest.raises(ValueError, match=r"max_comment_chars"):
        resolve_feedback(cfg)


def test_shipped_config_has_a_feedback_block_with_the_documented_knobs():
    # The block is the contract with whoever does the handoff: an enable flag, the destination,
    # and the caps. Shipped with an EMPTY address on purpose - see resolve_feedback.
    block = CONFIG["feedback"]
    assert set(block) == {
        "enabled",
        "notify_email",
        "max_comment_chars",
        "max_body_bytes",
        "max_sources",
    }, block
    assert block["enabled"] is True
    assert block["notify_email"] == ""


# --- Sign-in gate on POST /query (TEMPORARY: removed at go-live) ----------------
#
# The public demo link is unauthenticated Bedrock spend, and a link travels. These tests pin
# the shape of the gate, not just its presence: which route it covers, that it trusts only
# this pool and this client, and that no credential is anywhere near the template.


def _routes(template):
    """RouteKey -> route properties, for the HTTP API's routes."""
    return {
        r["Properties"]["RouteKey"]: r["Properties"]
        for r in template.find_resources("AWS::ApiGatewayV2::Route").values()
    }


def test_query_is_gated_by_a_jwt_authorizer_and_the_open_routes_stay_open():
    template = _template()
    routes = _routes(template)

    query = routes["POST /query"]
    assert query["AuthorizationType"] == "JWT", query
    assert "AuthorizerId" in query, query

    # /warm fires on page load, before anyone could have signed in, so gating it would only
    # guarantee a cold first query. It runs no generation call.
    assert routes["GET /warm"].get("AuthorizationType", "NONE") == "NONE", routes["GET /warm"]

    # Exactly one route is gated. A second one would mean something was gated by accident.
    gated = [key for key, props in routes.items() if props.get("AuthorizationType") == "JWT"]
    assert gated == ["POST /query"], gated


def test_the_authorizer_is_native_and_trusts_only_this_pool_and_this_client():
    template = _template()
    (auth,) = template.find_resources("AWS::ApiGatewayV2::Authorizer").values()
    props = auth["Properties"]

    assert props["AuthorizerType"] == "JWT", props
    # No authorizer Lambda: nothing to cold-start, nothing to pay for, no code of ours in the
    # auth decision. A JWT authorizer has no AuthorizerUri.
    assert "AuthorizerUri" not in props, props
    assert props["IdentitySource"] == ["$request.header.Authorization"], props

    # The issuer is built from THIS stack's pool, at deploy time - never a pasted pool id.
    issuer = json.dumps(props["JwtConfiguration"]["Issuer"])
    assert "cognito-idp." in issuer and "AWS::Region" in issuer, issuer
    (pool_lid,) = template.find_resources("AWS::Cognito::UserPool").keys()
    assert pool_lid in issuer, issuer

    # AUDIENCE IS THE APP CLIENT ID. A Cognito ACCESS token carries no `aud`, it carries
    # `client_id`, and API Gateway "validates client_id only if aud is not present" - which is
    # what makes the access token the widget sends validate against this entry.
    (client_lid,) = template.find_resources("AWS::Cognito::UserPoolClient").keys()
    assert props["JwtConfiguration"]["Audience"] == [{"Ref": client_lid}], props


def test_the_app_client_is_public_password_auth_and_expires_in_a_day():
    template = _template()
    (client,) = template.find_resources("AWS::Cognito::UserPoolClient").values()
    props = client["Properties"]

    # No secret: the widget is JavaScript in a browser, so a secret would be readable by anyone
    # who views source, and Cognito refuses the unsigned browser call unless the client is public.
    assert props["GenerateSecret"] is False, props
    # USER_PASSWORD_AUTH is what a dependency-free widget can do without SRP's big-integer crypto.
    # ALLOW_REFRESH_TOKEN_AUTH is added by Cognito; the widget drops the refresh token unread.
    assert set(props["ExplicitAuthFlows"]) == {
        "ALLOW_USER_PASSWORD_AUTH",
        "ALLOW_REFRESH_TOKEN_AUTH",
    }, props
    # One sign-in covers the session, and one day is also Cognito's ceiling for an access token.
    assert props["TokenValidityUnits"]["AccessToken"] == "minutes", props
    assert props["AccessTokenValidity"] == 24 * 60, props
    # The refresh token the widget throws away cannot outlive the session by the 30-day default.
    assert props["RefreshTokenValidity"] == 24 * 60, props
    assert props["PreventUserExistenceErrors"] == "ENABLED", props


def test_the_pool_holds_one_admin_created_account_keyed_by_email():
    template = _template()
    (pool,) = template.find_resources("AWS::Cognito::UserPool").values()
    props = pool["Properties"]

    # UsernameAttributes, NOT AliasAttributes, and the distinction is load-bearing for the CLI
    # step: in ALIAS mode Cognito rejects a username of email format, so the documented
    # `admin-create-user --username someone@example.com` would fail outright.
    assert props["UsernameAttributes"] == ["email"], props
    assert "AliasAttributes" not in props, props
    # Nobody signs themselves up.
    assert props["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True, props
    # The gate leaves with `cdk destroy`, and with the code deletion at go-live.
    assert pool.get("DeletionPolicy") == "Delete", pool


def test_the_gate_ships_no_credentials_and_takes_no_password_parameter():
    # A password in a template is a password in the console, the changeset and the stack events.
    # The account is created by hand after deploy - which is why the stack prints the commands.
    template = _template()
    body = template.to_json()
    assert body.get("Parameters", {}).get("DemoAuthPassword") is None, "no password parameter"
    for key in ("Password", "TemporaryPassword", "MFAConfiguration"):
        assert f'"{key}":' not in json.dumps(
            template.find_resources("AWS::Cognito::UserPool")
        ), key
    # No user is created in CloudFormation at all.
    template.resource_count_is("AWS::Cognito::UserPoolUser", 0)
    # The deployer runs both steps with their own credentials, so both are printed - including
    # --permanent, without which sign-in returns a challenge instead of a token.
    outputs = template.find_outputs("*")
    create = outputs["DemoAuthCreateUserCommand"]["Value"]
    set_pw = outputs["DemoAuthSetPasswordCommand"]["Value"]
    assert "admin-create-user" in json.dumps(create), create
    assert "--permanent" in json.dumps(set_pw), set_pw


def test_the_embed_tag_and_the_demo_page_both_carry_the_sign_in_ids():
    # An embed without them renders a widget that cannot obtain a token and 401s on every
    # question. Both ids are public by design: they name the pool, they do not open it.
    template = _template()
    tag = json.dumps(template.find_outputs("WidgetEmbedTag"))
    assert "data-user-pool-id" in tag and "data-client-id" in tag, tag

    page = (
        pathlib.Path(__file__).resolve().parents[3] / "frontend" / "demo-site.html"
    ).read_text(encoding="utf-8")
    assert 'data-user-pool-id="__USER_POOL_ID__"' in page
    assert 'data-client-id="__CLIENT_ID__"' in page
    # ...and they are stamped at deploy, never hardcoded - see the marker test above.
    assert "us-west-2_" not in page, "no literal pool id may be committed to the page"


def test_the_gate_does_not_touch_the_demo_distribution():
    # HARD CONSTRAINT: the demo distribution's live alias and certificate exist only on the
    # deployed resource, not in this template, so ANY update to it strips them. The gate reaches
    # the demo page through its BucketDeployment (an S3 object write plus an invalidation), and
    # an invalidation is not an update to the distribution resource.
    template = _template()
    demo = _distribution(template, "index.html")["Properties"]["DistributionConfig"]
    blob = json.dumps(demo)
    for needle in ("Cognito", "DemoAuth", "Authorizer", "user-pool"):
        assert needle not in blob, (needle, blob)
    # The demo page still reaches /query cross-origin, which now needs the Authorization header
    # through the preflight - that lives on the API's CORS config, not on the distribution.
    api = _one(template, "AWS::ApiGatewayV2::Api")
    assert "Authorization" in api["Properties"]["CorsConfiguration"]["AllowHeaders"]
