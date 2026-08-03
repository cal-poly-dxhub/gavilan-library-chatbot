"""Gavilan Library Chatbot - AWS architecture diagram.

Source of truth: this .py file. The PNG next to it (architecture_diagram.png) is the
generated artifact - regenerate it after editing this file:

    python docs/architecture_diagram.py

Requires Graphviz (`brew install graphviz`) and the `diagrams` library (`pip install
diagrams`).

Every node/edge below reflects the ACTUAL code (infra/infra/infra_stack.py, config.yaml,
app/handler.py, scraper/):

  - Ingestion: a scraper Lambda fetches the curated library seed URLs, extracts clean
    markdown, uploads it to the KB source S3 bucket, and triggers a KB ingestion job. In the
    same run it regenerates the database catalog from databases.php (HTML parse + a Sonnet
    enrichment call) and writes it to a dedicated catalog S3 bucket. Runs on a weekly
    EventBridge schedule and on the one-click deploy Trigger.
  - Knowledge Base: CfnKnowledgeBase VECTOR, embedding amazon.titan-embed-text-v2:0 (1024
    dims); FIXED_SIZE chunking on the S3 data source.
  - Vector store: Amazon S3 Vectors (CfnVectorBucket + CfnIndex, cosine, float32). The KB
    WRITES to it during ingestion and READS from it during query.
  - Query path: API Gateway HTTP API v2 (POST /query) -> Lambda python3.13. The request
    carries a multi-turn messages array (trimmed to the last 10 turns server-side). The handler
    runs an agentic Bedrock Converse tool-use loop (run_agent) with FOUR tools:
    search_library_info (KB Retrieve), database_catalog (reads the catalog from S3), and the two
    live catalog tools search_book_catalog + search_course_reserves, which call the EXTERNAL Ex
    Libris Primo discovery API directly (a search plus a per-record availability/delivery call).
    So the query Lambda now reaches a third party on the hot path - it is no longer fully
    AWS-internal; each Primo call is timed out and soft-fails. NO guardrail is attached to the
    Converse call - the answer is screened by nothing but the system prompt.
  - Widget delivery: private S3 bucket (widget.js) fronted by a CloudFront distribution with
    an OAC-secured origin, built in the SAME stack. Separate concern from the query path:
    CloudFront + S3 deliver the widget CODE to the browser; the injected widget then uses the
    API Gateway query path.

Bedrock Guardrails is a real current component, but a deliberately narrow one: ONE guardrail
screening the INPUT for PROMPT_ATTACK before the loop, with no other content filter, no PII
policy, and nothing attached to Converse. WAF is NOT deployed today, so it alone remains in
the "Planned" group.
"""

import os

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.general import Client, InternetAlt1, Users
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import APIGateway, CloudFront
from diagrams.aws.security import Shield, WAF
from diagrams.aws.storage import S3

# Write the PNG next to this file (docs/) regardless of the current working directory.
_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "architecture_diagram")

graph_attr = {"fontsize": "20", "labelloc": "t", "pad": "0.5", "nodesep": "0.8", "ranksep": "1.1"}

with Diagram(
    "Gavilan Library Chatbot - AWS Architecture",
    filename=_OUTPUT,
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    # External library website the scraper pulls from.
    library_site = InternetAlt1("Library website\n(curated seed URLs)")

    # External (non-AWS) Ex Libris Primo discovery API the two live catalog tools call on the
    # query hot path - the query Lambda is no longer fully AWS-internal.
    primo_api = InternetAlt1("Primo / Ex Libris\nDiscovery API\n(external, non-AWS)")

    with Cluster("Ingestion  (weekly schedule + on deploy)"):
        scraper = Lambda("Scraper Lambda\n(fetch + extract;\nregenerate catalog)")
        source_bucket = S3("KB source bucket\n(markdown)")
        catalog_bucket = S3("Catalog bucket\n(database_catalog.json)")
        titan = Bedrock("Titan Text\nEmbeddings v2\n(1024-dim)")

    # The KB + vector store are the hub shared by BOTH flows.
    with Cluster("Shared RAG core  (Bedrock KB + S3 Vectors)"):
        kb = Bedrock("Bedrock\nKnowledge Base\n(S3 data source,\nFIXED_SIZE chunk)")
        s3vectors = S3("S3 Vectors\nbucket + index\n(1024-dim, cosine)")

    with Cluster("Query  (per request, runtime)"):
        student = Users("Student")
        widget = Client("Chat widget\n(embedded JS,\ninjected into page)")
        api = APIGateway("API Gateway\nHTTP API v2\nPOST /query")
        query_fn = Lambda("Lambda (python3.13)\nagentic Converse\ntool-use loop")
        guardrails = Shield("Bedrock Guardrail\ninput screen\n(PROMPT_ATTACK only)")
        claude = Bedrock("Claude Sonnet 4.6\n(Converse)\ngeneration")

    # Widget CODE delivery. SEPARATE concern from the query flow: CloudFront + S3 ship
    # widget.js to the browser; the injected widget then rides the query path.
    with Cluster("Widget delivery  (CloudFront + S3, OAC origin)"):
        host_page = InternetAlt1("Library web page\n(host site)\n<script src=.../widget.js>")
        cdn = CloudFront("CloudFront\ndistribution\n(OAC origin)")
        widget_bucket = S3("Private S3 bucket\nwidget.js\n(CloudFront-only)")

    with Cluster("Planned  (not deployed today)", graph_attr={"style": "dashed", "bgcolor": "gray95"}):
        waf = WAF("AWS WAF")

    # --- Ingestion flow: scrape -> S3 markdown + catalog -> KB ingest -> embed -> WRITE vectors ---
    library_site >> Edge(color="darkorange", label="scrape\nseed URLs") >> scraper
    scraper >> Edge(color="darkorange", label="upload\nmarkdown") >> source_bucket
    scraper >> Edge(color="darkorange", label="parse + enrich\n-> catalog JSON") >> catalog_bucket
    source_bucket >> Edge(color="darkorange", label="S3 data source\ningest (FIXED_SIZE)") >> kb
    kb >> Edge(style="dashed", label="embed chunks") >> titan
    kb >> Edge(color="darkorange", label="write vectors") >> s3vectors

    # --- Query flow: widget -> API GW -> Lambda -> agentic tool-use loop ---
    student >> Edge(color="darkblue", label="asks question") >> widget
    widget >> Edge(color="darkblue", label="POST /query") >> api
    api >> Edge(color="darkblue", label="proxy\n(payload 2.0)") >> query_fn
    # Tool 1 - search_library_info: KB Retrieve (which READS the vector store).
    query_fn >> Edge(color="darkblue", label="search_library_info\n(KB Retrieve)") >> kb
    kb >> Edge(color="darkblue", style="dashed", label="read vectors") >> s3vectors
    # Tool 2 - database_catalog: read the catalog from S3.
    query_fn >> Edge(color="darkblue", style="dashed", label="database_catalog\n(read catalog)") >> catalog_bucket
    # Tools 3 + 4 - the live Primo tools: outbound HTTPS to an EXTERNAL third party on the hot
    # path (search + a per-record availability/delivery call), timed out and soft-failing.
    query_fn >> Edge(color="darkblue", style="dashed", label="search_book_catalog\n(Primo search + availability)") >> primo_api
    query_fn >> Edge(color="darkblue", style="dashed", label="search_course_reserves\n(Primo search + availability)") >> primo_api
    # The guardrail screens the QUESTION once, before the loop starts. Generation itself is
    # unscreened: the Converse call carries no guardrailConfig.
    query_fn >> Edge(style="dashed", label="ApplyGuardrail\n(source=INPUT, once)") >> guardrails
    query_fn >> Edge(style="dashed", dir="both", label="Converse\n(each loop turn)") >> claude

    # --- Response back to the user ---
    query_fn >> Edge(color="darkgreen", style="dotted", label="{answer, sources}\n(via API GW + widget)") >> student

    # --- Widget delivery flow: how the widget CODE reaches the browser (purple) ---
    student >> Edge(color="purple", label="visits page") >> host_page
    host_page >> Edge(color="purple", label="<script> GET\nwidget.js") >> cdn
    cdn >> Edge(color="purple", style="dashed", label="origin fetch\n(OAC, private)") >> widget_bucket
    cdn >> Edge(color="purple", style="dotted", label="serves widget.js\n(cached at edge)") >> host_page
    host_page >> Edge(color="purple", style="dashed", label="widget injects,\nthen POST /query") >> widget

    # --- Planned components (drawn distinct; no real traffic today) ---
    waf >> Edge(color="gray", style="dotted", label="planned") >> api
