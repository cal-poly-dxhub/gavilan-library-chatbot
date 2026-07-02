"""Gavilan Library Chatbot - AWS architecture diagram.

Source of truth: this .py file. The PNG next to it (architecture_diagram.png) is the
generated artifact. Render with:

    python docs/architecture_diagram.py

Requires Graphviz (`brew install graphviz`) and the `diagrams` library (`pip install
diagrams`).

Every node/edge below was verified against the ACTUAL deployed code on the
feat/cdk-skeleton branch (infra/infra/infra_stack.py, config.yaml, app/handler.py), not
from memory:

  - Web Crawler data source: CfnDataSource type WEB, seed https://www.gavilan.edu/library/,
    include/exclude filters, HOST_ONLY scope.
  - Knowledge Base: CfnKnowledgeBase VECTOR, embedding amazon.titan-embed-text-v2:0
    (Titan Text Embeddings v2, 1024 dims); FIXED_SIZE chunking on the data source.
  - Vector store: OpenSearch Serverless VECTORSEARCH collection + knn_vector index. The KB
    WRITES to it during ingestion and READS from it during query.
  - Query path: API Gateway HTTP API v2 (POST /query) -> Lambda python3.13. The handler
    does step 1 Retrieve (KB Retrieve) then step 2 Generate (Bedrock Converse to
    anthropic.claude-3-5-haiku-20241022-v1:0).
  - Widget delivery: private S3 bucket (widget.js) fronted by a CloudFront distribution
    with an OAC-secured origin, plus a BucketDeployment that uploads widget.js. Built in
    THIS stack (infra/infra/infra_stack.py: WidgetBucket, WidgetDistribution,
    S3BucketOrigin.with_origin_access_control). This is a SEPARATE concern from the query
    path: CloudFront + S3 deliver the widget CODE to the browser; the injected widget then
    uses the existing API Gateway query path for actual questions.

Bedrock Guardrails IS built now: a guardrail (content filters + PII) wraps the Lambda's
Bedrock Converse call, screening the input before generation and the output after, so it is
drawn as a real current component on the generation step (not planned). WAF is still NOT
deployed today, so it alone remains in the distinct "Planned" group (per the stack
docstring). Widget hosting IS built too, so the widget and its CDN are real current
components.
"""

import os

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.analytics import ElasticsearchService  # OpenSearch/Elasticsearch icon
from diagrams.aws.compute import Lambda
from diagrams.aws.general import Client, InternetAlt1, Users
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import APIGateway, CloudFront
from diagrams.aws.security import Shield, WAF
from diagrams.aws.storage import S3

# Write the PNG next to this file (docs/) regardless of the current working directory.
_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "architecture_diagram")

# Edge color/style conventions.
INGEST = Edge(color="darkorange")          # ingestion data flow
QUERY = Edge(color="darkblue")             # query request flow
MODEL_CALL = Edge(style="dashed")          # a Bedrock foundation-model invocation
RESPONSE = Edge(color="darkgreen", style="dotted")  # response back to the user
DELIVERY = Edge(color="purple")            # widget-code delivery (CloudFront + S3)
PLANNED = Edge(color="gray", style="dotted", label="planned")

graph_attr = {"fontsize": "20", "labelloc": "t", "pad": "0.5", "nodesep": "0.8", "ranksep": "1.1"}

with Diagram(
    "Gavilan Library Chatbot - AWS Architecture (as deployed on feat/cdk-skeleton)",
    filename=_OUTPUT,
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    # External library website that the crawler seeds from.
    library_site = InternetAlt1("Library website\n(seed URLs)")

    with Cluster("Ingestion  (scheduled sync, offline)"):
        crawler = Bedrock("Web Crawler\ndata source\n(WEB, HOST_ONLY)")
        titan = Bedrock("Titan Text\nEmbeddings v2\n(1024-dim)")

    # The KB + vector store are the hub shared by BOTH flows.
    with Cluster("Shared RAG core  (Amazon Bedrock KB + vector store)"):
        kb = Bedrock("Bedrock\nKnowledge Base\n(FIXED_SIZE chunk)")
        opensearch = ElasticsearchService(
            "OpenSearch Serverless\nvector collection + index"
        )

    with Cluster("Query  (per request, runtime)"):
        student = Users("Student")
        widget = Client("Chat widget\n(embedded JS,\ninjected into page)")
        api = APIGateway("API Gateway\nHTTP API v2\nPOST /query")
        query_fn = Lambda("Lambda (python3.13)\nretrieve + generate")
        guardrails = Shield("Bedrock\nGuardrails\n(content + PII)")
        claude = Bedrock("Claude\n(Converse)\ngeneration")

    # Widget CODE delivery. SEPARATE concern from the query flow above: CloudFront + S3
    # ship widget.js to the browser; the injected widget then rides the query path.
    with Cluster("Widget delivery  (deployed: CloudFront + S3, OAC origin)"):
        host_page = InternetAlt1("Library web page\n(host site)\n<script src=.../widget.js>")
        cdn = CloudFront("CloudFront\ndistribution\n(OAC origin)")
        widget_bucket = S3("Private S3 bucket\nwidget.js\n(CloudFront-only)")

    with Cluster("Planned  (not deployed today)", graph_attr={"style": "dashed", "bgcolor": "gray95"}):
        waf = WAF("AWS WAF")

    # --- Ingestion flow: crawl -> parse/chunk -> embed -> WRITE vectors ---
    library_site >> Edge(color="darkorange", label="crawl seeds\n+ child links") >> crawler
    crawler >> Edge(color="darkorange", label="Smart Parsing\n+ FIXED_SIZE chunk") >> kb
    kb >> Edge(style="dashed", label="embed chunks") >> titan
    kb >> Edge(color="darkorange", label="write vectors") >> opensearch

    # --- Query flow: widget -> API GW -> Lambda -> (Retrieve, Generate) ---
    student >> Edge(color="darkblue", label="asks question") >> widget
    widget >> Edge(color="darkblue", label="POST /query") >> api
    api >> Edge(color="darkblue", label="proxy\n(payload 2.0)") >> query_fn
    query_fn >> Edge(color="darkblue", label="1. Retrieve") >> kb
    # Same KB, but now READING the store (distinct from the ingestion write above).
    kb >> Edge(color="darkblue", style="dashed", label="read vectors") >> opensearch
    # Generation is screened by Bedrock Guardrails: the Converse call passes through the
    # guardrail, which checks the input before generation and the output after.
    query_fn >> Edge(style="dashed", label="2. Generate (Converse)") >> guardrails
    guardrails >> Edge(style="dashed", dir="both", label="screen input\n+ output") >> claude

    # --- Response back to the user ---
    query_fn >> Edge(color="darkgreen", style="dotted", label="answer JSON\n(via API GW + widget)") >> student

    # --- Widget delivery flow: how the widget CODE reaches the browser (purple) ---
    # Separate from the query flow: this ships widget.js; the injected widget then queries.
    student >> Edge(color="purple", label="visits page") >> host_page
    host_page >> Edge(color="purple", label="<script> GET\nwidget.js") >> cdn
    cdn >> Edge(color="purple", style="dashed", label="origin fetch\n(OAC, private)") >> widget_bucket
    cdn >> Edge(color="purple", style="dotted", label="serves widget.js\n(cached at edge)") >> host_page
    # The delivered widget injects into the host page and then uses the EXISTING query path.
    host_page >> Edge(color="purple", style="dashed", label="widget injects,\nthen POST /query") >> widget

    # --- Planned components (drawn distinct; no real traffic today) ---
    waf >> Edge(color="gray", style="dotted", label="planned") >> api
