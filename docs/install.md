# Install and setup

This is the whole procedure for putting this chatbot into your own AWS account: what to have
ready, what to change before the first deploy, how to deploy, what to paste on your website,
and how to hand the day-to-day settings to a librarian who will never see this repository.

It was built for Gavilan College Library, and a lot of it is still literally about Gavilan.
[What is Gavilan-specific](#what-is-gavilan-specific) lists every value you have to change and
what happens if you do not. Read that section before you deploy, not after. Several of the
wrong-value failures are silent: the deploy succeeds, the demo page works, and the thing that
is broken is the part you cannot see from your laptop.

[Known limitations](#known-limitations) collects the things this repository does not do. None
of them stop an install; all of them will surprise you later if nobody told you.

---

## Contents

1. [What you get](#what-you-get)
2. [Before you start](#before-you-start)
3. [What is Gavilan-specific](#what-is-gavilan-specific)
4. [Deploy](#deploy)
5. [Check the install actually worked](#check-the-install-actually-worked)
6. [The outputs this guide uses](#the-outputs-this-guide-uses)
7. [Put the widget on your website](#put-the-widget-on-your-website)
8. [Turn on the feedback email](#turn-on-the-feedback-email)
9. [Hand the settings to a librarian](#hand-the-settings-to-a-librarian)
10. [The hand-editing route](#the-hand-editing-route)
11. [Keeping the content fresh](#keeping-the-content-fresh)
12. [Known limitations](#known-limitations)
13. [Tearing it down](#tearing-it-down)

---

## What you get

One CDK stack, named `GavilanChatbotStack` (`infra/app.py`), containing:

- A Bedrock Knowledge Base over an Amazon S3 Vectors store, fed by a scraper Lambda that pulls
  a list of your library's web pages into an S3 bucket on a schedule.
- A query Lambda behind an HTTP API, running an agentic tool-use loop over four tools: knowledge
  base retrieval, a research-database catalog, a live book-catalog search and a live
  course-reserves search.
- An embeddable JavaScript chat widget on its own S3 bucket and CloudFront distribution.
- A hosted settings page where a librarian changes the widget's colour, font and starter
  questions without a redeploy and without an AWS login beyond their own.
- A shareable demo page on a second bucket and distribution, so one deploy hands you a working
  link before you have touched your own website.
- Optionally, a `POST /feedback` endpoint that emails a librarian when someone reports a wrong
  answer.

The full design is in [`architecture.md`](architecture.md). You do not need to read it to
install.

---

## Before you start

### An AWS account and credentials

You need credentials that can create IAM roles, Lambda functions, S3 buckets, CloudFront
distributions, Cognito user pools, SNS topics and Bedrock resources. Configure them however you
normally do (`aws configure`, an SSO profile, environment variables).

### A region

`infra/app.py` deploys the stack **environment-agnostically**: the `env=` argument is commented
out, so the stack goes to whatever account and region your AWS CLI resolves at deploy time. Set
that deliberately, for example `export AWS_REGION=us-west-2` or `--profile` on a profile whose
region you have checked.

Pick a US region. The generation model in `config.yaml` is a cross-region inference profile
(`us.anthropic.claude-sonnet-4-6`), and the `us.` prefix restricts it to US regions. The project
was built and its costs were measured in `us-west-2` (`config.yaml`, `cost_model.region`). Amazon
S3 Vectors and Bedrock Knowledge Bases are not available in every region, so confirm all three of
S3 Vectors, Bedrock Knowledge Bases and your chosen models in your region before you commit to
it. Moving later means a new stack, not an edit.

### Bedrock model access

This is the prerequisite that most often looks fine and is not. Model access is granted per
account, per region, in the Bedrock console under **Model access**. Grant all three of these in
your deploy region:

| Model id | Where it is configured | What breaks without it |
|---|---|---|
| `amazon.titan-embed-text-v2:0` | `config.yaml`, `knowledge_base.embedding_model_id` | Ingestion fails. The knowledge base stays empty and every answer is "I don't have that information". |
| `us.anthropic.claude-sonnet-4-6` | `config.yaml`, `generation.model_id` | `/query` fails at runtime. The widget shows an error. |
| `us.anthropic.claude-sonnet-4-6` | `config.yaml`, `catalog.enrichment_model_id` | The research-database catalog is never regenerated; the tool falls back to the bundled seed list in `app/data/database_catalog.json`. |

The stack writes the IAM policies for these itself, including the cross-region foundation-model
grants an inference profile needs. What it cannot do is grant *account-level model access* for
you. **Nothing about a missing grant fails the deploy** - see
[Check the install actually worked](#check-the-install-actually-worked).

### Tools on your machine

- **Python 3.11 or newer**, with `venv` and `pip`.
- **Node.js**, because the CDK CLI is an npm package.
- **The AWS CDK CLI**, pinned to `2.1129.0`: `npm install -g aws-cdk@2.1129.0`.
- **A working `pip` with network access at deploy time.** The scraper's dependency layer is built
  by downloading Linux wheels (`pip install --platform manylinux2014_x86_64 --only-binary=:all:`,
  `infra/infra/infra_stack.py`). If that fails for any reason, CDK falls back to bundling in
  Docker, so have Docker available or expect the deploy to stop there.

### A bootstrapped account and region

```bash
cdk bootstrap
```

Once per account and region. CDK needs its own staging bucket before it can upload the Lambda
assets.

### Outbound internet from the query Lambda

Two of the four tools call the Ex Libris Primo discovery API directly over HTTPS. That is a
third-party service, not an AWS API, so no IAM grant makes it work. The default deploy runs the
query Lambda outside a VPC, which has outbound internet. If you move it into a VPC it needs a NAT
path, or those two tools silently return "catalog unavailable" and the bot keeps answering
everything else.

---

## What is Gavilan-specific

Everything in this section is a value you should expect to change. The three at the top are the
ones where a wrong value deploys cleanly and fails invisibly.

### The three that fail quietly

**1. `cors.allow_origins` in `config.yaml` decides which website may talk to the API.**

This is the value most likely to be wrong in a way you will not notice. The shipped list is:

```yaml
cors:
  allow_origins:
    - https://www.gavilan.edu
    - http://localhost:8000
    - https://gavbot-demo.calpoly.io
```

The stack automatically appends its own demo-site distribution origin and its own widget
distribution origin at deploy time (`infra/infra/infra_stack.py`). That means **the demo page and
the settings editor work no matter what is in this list.** If you leave Gavilan's origin here and
paste the widget onto `https://library.example.edu`, the deploy succeeds, `cdk synth` passes, the
demo page answers questions perfectly, and the widget on your real site fails every request with
a browser CORS error. "Demo works, real embed dead" is the exact shape of this mistake.

Rules for entries: scheme plus host plus port, **no trailing slash and no path**. API Gateway
matches the full origin string exactly, and a browser sends a host-only `Origin`. So
`https://library.example.edu`, never `https://library.example.edu/` and never
`https://library.example.edu/library/`. A wildcard `*` is rejected at synth
(`infra/infra/config.py`, `resolve_cors_allow_origins`), deliberately: `/query` costs money per
call.

Delete `http://localhost:8000` before you go live. It only exists for `frontend/demo-live.html`.

**2. The Primo catalog identity is hardcoded in `app/handler.py`, and it does not fail loudly.**

The two live-catalog tools are pointed at Gavilan's Primo instance by constants in the handler,
not by `config.yaml`:

```python
PRIMO_SEARCH_URL    = "https://caccl-gavilan.primo.exlibrisgroup.com/primaws/rest/pub/pnxs"
PRIMO_DISCOVERY_URL = "https://caccl-gavilan.primo.exlibrisgroup.com/discovery/search"
PRIMO_INST          = "01CACCL_GAVILAN"
PRIMO_VID           = "01CACCL_GAVILAN:GAVILAN"
```

Only the behavioural knobs (`timeout_seconds`, `number_of_results`,
`availability_budget_seconds`) are in `config.yaml`, under `primo`. The comment in that block says
so: "The endpoint identity (host, inst, vid, scope) is tied to the institution and lives in code
(app/handler.py), not here".

**The failure mode is not an error. It is a wrong answer.** Gavilan's Primo is a public endpoint
with no authentication, so another institution's install will query it successfully and report
Gavilan's holdings and Gavilan's course reserves as its own, with Gavilan permalinks as the cited
sources. Change these four constants to your own Primo instance, or remove the two tools, before
anyone uses the bot. `PRIMO_SCOPE`, `PRIMO_TAB`, `PRIMO_RESERVES_SCOPE` and `PRIMO_RESERVES_TAB`
are in the same block and may also differ at your institution.

**3. The database catalog only regenerates for a page whose URL contains `databases.php`.**

`scraper/lambda_function.py` finds the page to parse by substring match:

```python
(r for r in results if r.ok and "databases.php" in r.url and r.html), None
```

If your A-Z research-databases page has a different filename, the scraper logs
`catalog: skipped (databases.php not in this tier)` and the `database_catalog` tool serves the
bundled Gavilan seed list from `app/data/database_catalog.json` forever. The parser itself
(`scraper.extract_database_catalog`) is also written against the structure of Gavilan's page, and
`catalog.min_databases: 30` in `config.yaml` is a guard sized for Gavilan's roughly 46 entries: a
site with fewer databases will trip the guard and keep the last-good catalog rather than write a
correct small one.

### Dead configuration: `data_source.web_crawler`

`config.yaml` still carries a `data_source.web_crawler` block with `seed_urls`,
`exclude_patterns`, `scope`, `max_pages` and `rate_limit`. **Nothing reads it.** The managed web
crawler was replaced by the custom scraper, and the only place in the repository that mentions
`data_source.web_crawler.seed_urls` is the old install prose in `README.md`.

The README also tells you to edit `scraper.seed_urls`. **That key does not exist.** The scraper
reads `scraper.tiers`, and `infra/infra/config.py` (`resolve_seed_urls`) builds the URL list by
flattening every tier's `urls`. Editing either of the keys the README names changes nothing at
all, and the deploy will not complain.

The pages your bot actually reads are the ones under `scraper.tiers.*.urls`. Nowhere else.

### The rest, in one table

| Where | What it is | If you leave it |
|---|---|---|
| `config.yaml` `scraper.tiers.fast.urls` | Three Gavilan pages with hours and dated closures, scraped daily | Your bot answers hours questions from Gavilan's hours |
| `config.yaml` `scraper.tiers.full.urls` | The rest of the Gavilan corpus, scraped every five days | Same, for everything else. Every seed URL must belong to exactly one tier and a `full` tier must exist, or synth fails |
| `config.yaml` `scraper.kb_exclude_urls` | Pages fetched but never indexed. Currently Gavilan's `databases.php` | Harmless, but only meaningful once the URL matches one of yours |
| `config.yaml` `scraper.user_agent` | Identifies the scraper to your web server | Your site logs see `GavilanLibraryScraper/1.0` |
| `config.yaml` `feedback.notify_email` | Empty on purpose. The librarian mailbox for wrong-answer reports | No `/feedback` endpoint is created and the deploy prints a `FeedbackStatus` output saying why. See [Turn on the feedback email](#turn-on-the-feedback-email) |
| `config.yaml` `knowledge_base.name` | `gavilan-library-kb` | Cosmetic |
| `config.yaml` `vector_store.vector_bucket_name` | `gavilan-library-vectors` | Cosmetic, but rename it so your console is legible |
| `config.yaml` `guardrail.name` | `gavilan-library-input-guardrail` | Cosmetic |
| `config.yaml` `guardrail.blocked_input_messaging` | The bilingual message shown when the input screen blocks a message. Names "the Gavilan College Library" | Students see another college's name. Bedrock caps this at 500 characters and it only takes effect on the next `cdk deploy` |
| `config.yaml` `catalog.min_databases` | `30`, a guard sized for Gavilan's ~46 databases | A smaller library trips the guard and keeps the bundled catalog |
| `config.yaml` `retrieval.number_of_results` | `8`, tuned against Gavilan's corpus | Works, but is not tuned for yours |
| `config.yaml` `chunking` | `FIXED_SIZE`, 600 tokens, tuned against Gavilan's pages | Works. Note that changing it **replaces** the Bedrock data source, and the replacement starts empty. See the "Changing `chunking` in config.yaml" section of `CLAUDE.md` before you touch it |
| `config.yaml` `cost_model.measured` | Real token counts measured against Gavilan's deployment on 2026-07-29 | The demo page's cost estimate is somebody else's. Re-measure with `eval/measure_usage.py` |
| `config.yaml` `demo_site.enabled` | `true` | You publish a public demo page. Set `false` once you are live and the next deploy removes the bucket, the distribution and the extra CORS origin |
| `app/system_prompt.md` | The bot's identity ("GAVBot, the Gavilan College Library assistant"), its scope, and an emergency response block containing **a real phone number**, `(408) 848-4703`, and a Gavilan public-safety URL | Your bot introduces itself as another college's and, in the one situation that matters most, gives a student a phone number for a campus 60 miles away. Change this first |
| `app/data/library_links.json` | The curated table of canonical URLs injected into every request: library home, college site, bookstore, research guides, campus maps, public safety, interlibrary loan. All `gavilan.edu`, `gavilan.libguides.com`, `gavilan.bkstr.com` and Gavilan Primo permalinks | The bot confidently cites another institution's pages. Edit the file and redeploy; the filename is `library_links.data_file` in `config.yaml` |
| `app/data/database_catalog.json` | The hand-authored not-held list, a fallback held list and `catalog_url`, all Gavilan's | The bot tells your students which databases Gavilan does and does not have |
| `frontend/widget.js`, the `STRINGS` table | Every user-visible string, in English and Spanish, including the greeting "Hi! I'm the Gavilan College Library assistant" | Your widget greets people as Gavilan. The table is above the `END LOCALIZATION` banner; nothing below that banner contains copy |
| `frontend/defaults/theme.json` | The default maroon `#8a1c30`, the font, and the four starter questions per language | These are the defaults a librarian sees the first time they open the settings editor. They can change all of it themselves later, without a deploy |
| `frontend/demo-site.html` | A Gavilan-Library-styled sample page, with Gavilan wordmarks and outbound links to `gavilan.edu` | Your demo page pretends to be Gavilan's library. Set `demo_site.enabled: false` or restyle it |

---

## Deploy

All infra commands run from `infra/` with the virtualenv active.

```bash
cd infra
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Check it synthesizes before you spend anything. `cdk synth` needs no AWS credentials and no
network, and it is where a bad `cors.allow_origins`, a malformed `feedback.notify_email` or a
broken `scraper.tiers` block will stop you:

```bash
cdk synth
```

Run the unit tests. They stub boto3 and need no live AWS:

```bash
python -m pytest
```

Then deploy:

```bash
cdk deploy
```

CDK will show you the IAM changes and ask for confirmation. Expect the first deploy to take a
long time: it creates two CloudFront distributions, and CloudFront is slow to create and to
destroy, roughly 15 to 30 minutes each way. That dominates the wall clock.

When it finishes, CDK prints the stack outputs. You can always read them again:

```bash
aws cloudformation describe-stacks --stack-name GavilanChatbotStack \
  --query "Stacks[0].Outputs" --output table
```

---

## Check the install actually worked

**`cdk deploy` returns before your content is indexed, and it cannot tell you whether it worked.**

The install-time scrape runs through a CDK `Trigger` with `invocation_type=EVENT`, which is
fire-and-forget: the deploy succeeds the moment the scraper Lambda is *invoked*, regardless of
what the scrape does. The scraper then calls `StartIngestionJob`, which is itself asynchronous.
So a deploy over an account with no Bedrock model access looks exactly like a clean install:
green CloudFormation, all outputs present, a demo page that loads, and a knowledge base with zero
documents in it. The symptom, minutes later, is a bot that answers every question with some form
of "I don't have that information".

Do these three checks after every first deploy.

**1. Is the API alive?** `GET /warm` is not gated and does one knowledge-base retrieval:

```bash
curl "$(aws cloudformation describe-stacks --stack-name GavilanChatbotStack \
  --query "Stacks[0].Outputs[?OutputKey=='ChatbotApiUrl'].OutputValue" --output text \
  | sed 's#/query$#/warm#')"
```

`{"warmed": true}` means API Gateway, the Lambda, its IAM and the retrieval path all work. It does
**not** mean the knowledge base has anything in it: a retrieval against an empty index succeeds
and returns nothing.

**2. Did ingestion actually run, and did it succeed?** This is the check that catches a missing
model grant.

```bash
KB=$(aws cloudformation describe-stacks --stack-name GavilanChatbotStack \
  --query "Stacks[0].Outputs[?OutputKey=='KnowledgeBaseId'].OutputValue" --output text)
aws bedrock-agent list-data-sources --knowledge-base-id $KB
aws bedrock-agent list-ingestion-jobs --knowledge-base-id $KB --data-source-id <DATA_SOURCE_ID>
```

You want a job with status `COMPLETE` and a non-zero document count. `FAILED` with an access
error means the Titan embeddings grant is missing. No jobs at all means the scraper never ran or
never uploaded anything: read its CloudWatch logs, where every run emits one structured
`scrape run summary` line with pages fetched, changed, failed, and the ingestion decision.

**3. Does it answer?** Open the `DemoSiteUrl` output and ask it something only your library's
pages could answer, such as your opening hours. This exercises the real delivery path: the demo
page loads the same `widget.js` from the same distribution your website will, and posts to the
same endpoint.

If ingestion completed but answers are still empty, check that `scraper.tiers.*.urls` actually
point at your pages and not Gavilan's.

---

## The outputs this guide uses

`cdk deploy` prints several outputs. These are the ones this guide refers to. Copy values from
the outputs rather than from anywhere else: bucket names, distribution domains, pool ids and API
ids are all generated per install, so nothing here can be written down in advance.

| Output | What to do with it |
|---|---|
| `WidgetEmbedTag` | Paste it verbatim into your library web page. See below |
| `WidgetCdnDomain` | The CloudFront domain serving `widget.js`. Useful for debugging; the embed tag already contains it |
| `ChatbotApiUrl` | The `POST /query` endpoint. Useful for debugging; the embed tag already contains it |
| `WidgetThemeEditor` | The hosted settings page. This is the link you give the librarian |
| `WidgetThemeUpload` | An S3 console deep link to the widget bucket, for the manual theming route |
| `ThemeAdminCreateUserCommand` | A ready-to-run command that enrolls a librarian. See below |
| `KnowledgeBaseId` | The Bedrock knowledge base id, for the ingestion check above and for `eval/eval_config.yaml` |
| `FeedbackApiUrl` | The `POST /feedback` endpoint, present only when `feedback.notify_email` is set |
| `FeedbackStatus` | Appears **instead of** `FeedbackApiUrl` when `feedback.enabled` is true but the address is empty, and explains why there is no endpoint |
| `DemoSiteUrl` | The shareable demo page, present when `demo_site.enabled` is true |

---

## Put the widget on your website

Copy the `WidgetEmbedTag` output and paste it into the HTML of the page that should carry the
chatbot, near the end of `<body>`. It is one `<script>` tag with a `defer` attribute; it injects
itself and needs nothing else. No stylesheet, no build step, no other file.

Paste it **as printed**. The tag carries the deployed endpoints as attributes, and those are
generated per install. Do not retype it, and do not copy one from documentation, a screenshot or
another install.

Two things to get right:

- **The origin of the page you paste it on must be in `cors.allow_origins`.** If it is not, the
  widget renders and every question fails in the browser. See
  [What is Gavilan-specific](#what-is-gavilan-specific).
- **The widget renders inside a Shadow DOM**, so your site's CSS cannot reach into it and its CSS
  cannot leak out. You do not need to reserve space or add a container.

If you want to see it working before you touch your real site, open `DemoSiteUrl`. That page
carries the same one-line embed a library page would.

---

## Turn on the feedback email

`POST /feedback` emails a librarian when someone reports a wrong answer, carrying the source URLs
the reported answer cited, because the fix is usually to edit one of those pages.

It is off until you give it a destination. In `config.yaml`:

```yaml
feedback:
  enabled: true
  notify_email: "library-web@example.edu"
```

Three outcomes, and the middle one is the trap:

- `enabled: false` builds nothing and says nothing.
- `enabled: true` with an **empty** address builds nothing and the deploy prints a
  `FeedbackStatus` output telling you why. This is the shipped default.
- `enabled: true` with a **malformed** address fails at synth, on purpose. A typo would otherwise
  create an SNS subscription nobody can ever confirm, which looks identical to a working install.

After the deploy, **SNS emails that address one confirmation link, and somebody has to click it.**
Until they do, every publish succeeds and nothing is delivered. Prefer a shared or role mailbox
over a person's, or the feature ends silently when they leave.

There is no server-side store for reports. The email is the record, by design. A failed publish
loses the report.

Note that the shipped `frontend/widget.js` has no report-an-answer button, so nothing in the
product calls this endpoint yet. See [Known limitations](#known-limitations).

---

## Hand the settings to a librarian

A librarian can change three things about the widget without a redeploy, without a repository and
without an AWS console login: the highlight colour, the font, and the starter questions offered
before anyone types. They do it on a hosted settings page.

Setting that up is **one command, once**, and everything after it is self-service.

### 1. Enroll them

Copy the `ThemeAdminCreateUserCommand` output. It looks like this, with your own region and pool
id already filled in:

```
aws cognito-idp admin-create-user --region <region> --user-pool-id <pool-id> \
  --username PROJECT_EMAIL_HERE \
  --user-attributes Name=email,Value=PROJECT_EMAIL_HERE Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
```

Replace `PROJECT_EMAIL_HERE` with the librarian's email address **in both places** - the address
is the username - and run it with your own AWS credentials. Do not remove any of the flags:

- `--desired-delivery-mediums EMAIL` is required. The default is SMS, and the invitation would
  never arrive.
- `Name=email_verified,Value=true` is what makes self-service password reset work later.
- There is no `--temporary-password`. Cognito generates one, which keeps a password out of your
  shell history.

For a second librarian, run the same command with a different address. For an invitation that has
expired, run it again with `--message-action RESEND`.

### 2. They get an invitation email

Cognito emails the address a message subject-lined "Your Gavilan Library chatbot settings sign-in"
containing a link to the settings page, their username, and a generated temporary password. It is
valid for **90 days**.

The stack configures no SES identity, so this goes out through Cognito's built-in email sender,
from an AWS no-reply address. Tell them to check their spam folder if it does not arrive.

### 3. They sign in and choose a password

The link in the email opens the settings editor. They choose **Sign in**, which sends them to
Cognito's hosted managed-login pages. On the first sign-in they are **required** to replace the
temporary password with their own. The password policy is 12 characters with upper case, lower
case, a digit and a symbol.

Nothing in this project hosts a sign-in form, a password-change form or a forgotten-password flow.
All three are Cognito's own pages. After the first sign-in, "Forgot password" works on its own,
by email, with no help from you.

### 4. They find the settings page

Its address is the `WidgetThemeEditor` output. That is the link to send them, and the one to put
in whatever internal documentation your library keeps. It is also the link in the invitation
email, so a librarian who keeps that message can always get back.

There is no other entry point, deliberately. Everything else about theming is reached from inside
this page.

### 5. They save a theme

The page's main view is three things: the fields, a live preview of the widget, and **Save**.

- **Highlight colour.** A colour picker, or a hex value typed in. Six digits (`#1e4b8f`) or three
  (`#a13`); colour names and `rgb()` are not read. They do not choose the text colour that sits on
  it: the widget derives black or white, whichever contrasts better.
- **Font.** One of five keywords: `system` (the default), `sans`, `serif`, `mono`, `inherit`.
  Family only, never size or weight. No fonts are downloaded, so the list is limited to what every
  Mac and Windows machine already has.
- **Starter questions.** Up to four per language, up to 120 characters each. Over-long entries are
  dropped rather than shortened. Spanish is optional; leaving it out means Spanish-speaking
  visitors see the English list, because nothing here is machine-translated.

Signed in, **Save publishes straight to the live widget**. There is no upload step and no
redeploy. The change is visible in about a minute, which is the cache lifetime on that file.

Not signed in, the same page still works as a builder: the button reads "Sign in to save", and the
**Settings** panel (top right) holds a **Download theme.json** button that produces a finished,
correctly named file. That panel also holds "Choose defaults", which refills the form with the
shipped values, the account controls (change password, change email, sign out), and the only link
to the manual guide.

Two things a custom colour is worth checking. The highlight is also used as a *text* colour on
light surfaces, for links, source links and the starter-question chips, so contrast there depends
on the colour chosen. A dark saturated brand colour is safe; a pale or bright one should be
checked against `#ffffff` and `#f1f3f6` with a contrast tool first. The accessibility audit in
[`accessibility-audit.md`](accessibility-audit.md) measured the default colours only.

---

## The hand-editing route

If the librarian is not signed in, or you would rather not enroll anyone, the same result is
reachable by editing a file and uploading it. This is the fallback, not the main path.

The widget reads a single file, `theme.json`, from the root of its own bucket, at startup. There
is no such file on a fresh install, and that is the normal state: the widget falls back entirely
to its built-in defaults, and emits no theme CSS at all.

The procedure:

1. Open the `WidgetThemeEditor` output, then **Settings**, then **Download theme.json**. That
   gives you a copy of the built-in defaults, already correctly named. The file documents itself:
   its `_readme` array lists every key, every font keyword and both caps, because a downloaded
   file travels alone.
2. Edit it in any text editor. Every key is optional; a file containing only `highlightColor`
   changes only the colour.
3. Open the `WidgetThemeUpload` output. It is a deep link straight to the widget bucket's object
   list in the S3 console, which matters because bucket names are generated and the demo-site
   bucket sits right next to this one. Upload `theme.json` at the **top level**, next to
   `widget.js`. Not into a folder.
4. Wait about a minute and reload a page carrying the widget.

The full customer-facing version of this procedure, including troubleshooting and how to revert,
is a hosted page: `theme-guide.html` at the widget bucket root, linked from the settings panel of
the editor. It is deliberately not linked anywhere else and has no stack output of its own.

Two things worth knowing about that file:

- **It survives `cdk deploy`.** The widget deployment carries an explicit exclusion for
  `theme.json` and `defaults/*`, so redeploying the stack does not delete a theme somebody
  uploaded.
- **Malformed JSON costs you the whole file, silently.** Each key is validated independently and
  a bad value falls back to the default, but a file that will not parse falls back entirely. There
  is no error message. If a change does not appear, that is the first thing to check.

The developer-facing version of all of this is [`widget-theming.md`](widget-theming.md).

---

## Keeping the content fresh

The scraper runs on two schedules, both declared in `config.yaml` under `scraper.tiers`, one
EventBridge rule per tier:

- **`fast`**, daily at 11:30 UTC. Only the pages listed under that tier. It exists so that a
  change to your opening hours reaches the bot within a day.
- **`full`**, at 10:00 UTC on the 1st, 6th, 11th, 16th, 21st and 26th. A full run fetches
  **every** URL in every tier, not just the ones listed under `full`. It is the complete refresh,
  it heals a failing fast tier, and it is the only run from which deleting stale content is safe.

Moving a page between tiers, or changing a cadence, is a config edit. There is no code change and
no new resource: the stack builds one rule per entry in that map.

Everything downstream is gated on whether content actually changed. A page whose content
fingerprint matches is not re-uploaded; an unchanged bucket starts no ingestion job; an unchanged
databases page runs no model call. A second run over unchanged content costs essentially nothing,
which is what makes a daily tier affordable.

One thing to know about redeploys: **`cdk deploy` does not re-scrape unless the scraper Lambda
itself changed.** The install trigger is tied to the scraper function's version, which hashes its
code and its configuration. A widget tweak, a demo-page edit or a query-Lambda change leaves that
hash identical, so CloudFormation does not re-run the trigger and no scrape fires. If you change
your seed URLs and want the content refreshed now, invoke the scraper Lambda by hand or wait for
the next scheduled run.

---

## Known limitations

Named here so you find them now instead of later.

1. **`README.md`'s configuration advice is wrong on the seed URLs.** It tells you to edit
   `data_source.web_crawler.seed_urls` or `scraper.seed_urls`. Nothing reads the first, and the
   second does not exist. The pages the bot reads are `scraper.tiers.*.urls` in `config.yaml`, and
   nothing else.
2. **Primo's institution identity is hardcoded and does not fail safe.** `app/handler.py` points
   the two live-catalog tools at Gavilan's public Primo instance. Another institution's install
   queries it *successfully* and reports Gavilan's holdings and course reserves as its own. These
   tools do not error; they answer, wrongly.
3. **A missing Bedrock model grant produces a clean-looking install.** `cdk deploy` returns before
   ingestion finishes and does not wait on its result, so a knowledge base that never indexed
   anything looks exactly like a working one from CloudFormation. Run the ingestion check above.
4. **The database catalog is keyed to a filename.** Regeneration only happens for a scraped URL
   containing `databases.php`, and the parser is written against Gavilan's page structure.
5. **The shipped widget has no report-an-answer button.** `POST /feedback` is fully built, tested
   and deployable, and its contract is documented, but nothing in `frontend/widget.js` calls it.
   Deploying it today gives you an endpoint your students have no way to reach.
6. **The demo page is a Gavilan pastiche.** `frontend/demo-site.html` carries Gavilan wordmarks,
   Gavilan styling and outbound links to `gavilan.edu`. It is labelled as a demo and served
   `noindex`, but if you deploy it unchanged you are publishing a page that looks like another
   college's library. Restyle it or set `demo_site.enabled: false`.
7. **The accessibility audit covers the default colours.** Text drawn *on* the highlight is safe at
   any colour because it is derived, but the highlight is also a text colour on light surfaces. A
   custom one is yours to verify.
8. **The Spanish copy has not had native review.** This is noted in `config.yaml` against
   `guardrail.blocked_input_messaging`, and applies to the widget's Spanish strings too.
9. **This is prototype code.** See the disclaimers at the top of `README.md`. Test it, review its
   security posture, and do not treat it as production-hardened because it deployed cleanly.

---

## Tearing it down

```bash
cd infra
source .venv/bin/activate
cdk destroy
```

This removes the S3 Vectors bucket and index, the knowledge base source bucket, the catalog
bucket, the widget bucket and the demo-site bucket, along with everything else in the stack.
Expect it to be slow for the same reason the deploy was: CloudFront takes 15 to 30 minutes to
delete.

**Anything a librarian uploaded goes with it**, including the `theme.json` holding their colour
and starter questions. Download a copy from the settings editor first if you want to keep it.
