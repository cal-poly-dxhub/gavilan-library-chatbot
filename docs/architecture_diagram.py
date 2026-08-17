"""Gavilan Library Chatbot - AWS architecture diagram.

Source of truth: this .py file. The PNG next to it (architecture_diagram.png) is the
generated artifact - regenerate it after editing this file:

    python docs/architecture_diagram.py

Requires Graphviz (`brew install graphviz`) and the `diagrams` library (`pip install
diagrams`).

Scope: the two paths that carry a student's question - ingestion and query - plus widget
delivery. The theme-save path, the feedback path and the demo site are real parts of the stack
and are deliberately not drawn; see docs/architecture.md for those.

Every node and edge reflects the ACTUAL code (infra/infra/infra_stack.py, config.yaml,
app/handler.py, scraper/):

  - Ingestion: the scraper Lambda fetches the curated seed URLs, extracts clean markdown,
    uploads it to the KB source bucket and triggers an ingestion job; the same run regenerates
    the database catalog from databases.php (HTML parse + a Sonnet enrichment call) into a
    dedicated bucket. One EventBridge rule per freshness tier - `fast` daily, `full` every five
    days - plus the one-click deploy Trigger. Every downstream step is change-gated: an
    unchanged page uploads nothing, an unchanged bucket starts no ingestion job, and unchanged
    database rows call no model.
  - Knowledge Base: CfnKnowledgeBase VECTOR, amazon.titan-embed-text-v2:0 (1024 dims),
    FIXED_SIZE chunking on the S3 data source, over S3 Vectors (cosine, float32). The KB writes
    to the index during ingestion and reads from it during query.
  - Query: API Gateway HTTP API v2 -> Lambda -> an agentic Converse tool-use loop over four
    tools. Two of them (search_book_catalog, search_course_reserves) call the EXTERNAL Ex Libris
    Primo API directly, so the query Lambda reaches a third party on the hot path; each call is
    timed out and soft-fails. The guardrail screens the INPUT for PROMPT_ATTACK once, before the
    loop - nothing is attached to Converse, so the answer is screened by the system prompt alone.
  - Widget delivery: a private S3 bucket fronted by CloudFront with an OAC-secured origin, in
    the same stack. A separate concern from the query path - this ships the widget CODE to the
    browser, and the injected widget then uses the query path.

Only deployed resources are drawn. WAF in particular is excluded from the architecture rather
than pending - it cannot attach to an HTTP API v2, and stage-level throttling is the real
cost-abuse control - so it does not belong on this diagram in any form.
"""

import os

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Lambda
from diagrams.aws.general import Client, InternetAlt1, Users
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import APIGateway, CloudFront
from diagrams.aws.security import Shield
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

    with Cluster("Ingestion  (tiered schedule + on deploy)"):
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
