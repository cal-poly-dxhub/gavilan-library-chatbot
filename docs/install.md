# GavBot install guide

## 1. In your AWS account

- Credentials that can create IAM, Lambda, S3, CloudFront.
- A US region (the generation model is a `us.` profile).
- Bedrock model access: `amazon.titan-embed-text-v2:0`
- Bedrock model access: `us.anthropic.claude-sonnet-4-6`
- Python 3.11+, Node.js, CDK CLI `2.1129.0`.
- `cdk bootstrap`, once per account and region.

Model access is granted in the Bedrock console, per region. Without it the deploy still
succeeds and every answer comes back empty.

## 2. Config values

One value in `config.yaml`, read at synth. Set it before you deploy.

**`cors.allow_origins`** - the origin of the page that will host the widget, for example
`https://library.example.edu`. Scheme and host only: no trailing slash, no path. Wrong value
and the widget renders but every question fails in the browser.

## 3. Deploy

```bash
cd infra
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt

cdk deploy
```

## 4. From the outputs

### `DemoSiteUrl`

*The fastest way to confirm the deploy worked.*

The deploy builds a sample library page with the widget already embedded, on its own
CloudFront distribution, and prints the link here. Open it and ask it your opening hours.
That is the end-to-end check that the knowledge base actually indexed your pages, and you
get it before putting anything on your live site.

**Take the URL from your own deploy output.** It is generated per install, so a link copied
from this guide or from someone else's install will not be yours. The page is kept out of
search results with a `noindex` meta tag and an `X-Robots-Tag` response header.

To remove it later, set `demo_site.enabled: false` in `config.yaml` and redeploy. That
deletes the page, its bucket and its distribution, and leaves the widget on your own site
untouched.

### `WidgetEmbedTag`

To test before you embed anything: the deploy creates a webpage with the live widget already
on it and prints the link as `DemoSiteUrl` above. Click that to confirm the bot works.

*Puts the bot on your site.*

Paste it verbatim into your page, just before `</body>`. It is one `<script>` tag with a
`defer` attribute: it injects itself and needs no stylesheet, no container and no build step.

**Paste it as printed.** The deployed endpoints are baked into its attributes and are
generated per install, so do not retype it and do not copy one from a screenshot or another
install.

The page's origin has to be in `cors.allow_origins`. Once it is live, ask it your opening
hours: that is the end-to-end check that the knowledge base actually indexed your pages.

## 5. The settings page

### 1. `ThemeAdminCreateUserCommand`

*Gives a librarian an account.*

Sets up the settings page login. Copy the printed command, replace `PROJECT_EMAIL_HERE` with
the librarian's address **in both places**, and run it once. Keep every flag as printed.

Cognito emails them an invitation with a generated temporary password, valid 90 days. The
first sign-in forces them to choose their own. Password changes and forgotten passwords are
self-service after that, so this is the only time you are involved.

Another librarian is the same command with another address. An expired invitation is the same
command plus `--message-action RESEND`.

### 2. `WidgetThemeEditor`

*Where the librarian takes over.*

Saving is gated on the login created in the previous step. A hosted page where the highlight
colour, the font and the starter questions are changed. Signed in, **Save publishes straight
to the live widget**: no upload, no redeploy, no AWS console, live in about a minute.

Signed out it still works as a builder, downloading a ready-to-upload `theme.json`. Everything
else about theming, including the manual guide, is reached from inside this page.
