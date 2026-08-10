# Restyling the widget without a redeploy

**Who this is for:** whoever owns the AWS account after handover. Nothing here needs a
developer, a build step, or a deploy.

Three things about the chat widget can be changed by uploading a single file:

| What | Key | Default |
|---|---|---|
| The highlight colour (header, launcher, Send, your own chat bubbles, links) | `highlightColor` | `#8a1c30` |
| The typeface | `fontFamily` | `system` |
| The example questions offered before anyone types | `starterQuestions` | the four shipped ones, per language |

Everything else - sizes, spacing, the panel layout, the wording of the chrome - is fixed.
See [Why so few knobs](#why-so-few-knobs).

---

## Doing it

Everything you need is in the AWS console, and both links come out of the stack itself -
every install generates its own bucket and CDN names, so there is nothing here to copy by
hand and no name to match by eye.

If you would rather not edit JSON at all, the `WidgetThemeEditor` output opens a hosted
form (colour picker, font choices, question fields, live preview). Signed in - your
sign-in was set up at handover, and the page's Sign in button handles passwords and
resets - its **Save** button puts the settings on the live widget directly, no upload
step at all. Without signing in it still downloads the finished `theme.json` for you,
replacing steps 1 and 2 below with the upload step the same as ever.

Open **CloudFormation** → the **GavilanChatbotStack** stack → the **Outputs** tab. Two rows
matter, and they are steps 1 and 2:

| Output | What it is |
|---|---|
| `WidgetThemeDownload` | a link that downloads `theme.json` |
| `WidgetThemeUpload` | a link that opens the bucket you upload it back into |

1. Click **`WidgetThemeDownload`**. It saves a file called `theme.json` - already the right
   name - holding the current defaults. (If your browser shows the JSON in a tab instead of
   saving it, use Save Page As and keep the name `theme.json`.)
2. Edit the values you want in any text editor. Leave the `_readme` line alone or delete it;
   the widget ignores it either way. **Then paste the whole file into a JSON validator**
   (jsonlint.com, or anything similar). This matters more than it sounds - see below.
3. Click **`WidgetThemeUpload`**. It opens the widget bucket in the S3 console, at the object
   list. You should see `widget.js` and a `defaults/` folder there; that is how you know it
   is the right bucket.
4. Choose **Upload**, add your `theme.json`, and upload it. Leave it at the top level, next
   to `widget.js` - not inside `defaults/`. You do not need to set permissions, metadata, or
   anything else on the object.
5. **Wait about a minute**, then reload a page that has the widget on it. Your colour should
   be there on the first paint.

To go back to the shipped look, delete `theme.json` from the top level of the bucket. The
copy under `defaults/` is not yours to manage - it is refreshed on every deploy and is only
ever the download in step 1.

### A few things worth knowing

- **Take the bucket from the `WidgetThemeUpload` link, never by name.** The account holds
  around nineteen buckets and the demo site's sits right next to the widget's. Uploading
  `theme.json` into the wrong one succeeds, reports nothing, and changes nothing - there is
  no error to see, only a widget that stays maroon. The link is the only reliable way in.
- **Malformed JSON fails silently.** One trailing comma, one missing quote, and the widget
  ignores the entire file and renders the defaults - no error on the page, nothing to
  notice. That is deliberate (a typo must never leave a blank widget on a library page), but
  it does mean a broken file and a file that has not arrived yet look identical. Validate
  before you upload; it takes ten seconds and it is the difference between the two.
- **60 seconds, twice over.** The CDN holds the file for 60 seconds and so does the browser,
  so allow about a minute end to end. If a change has not appeared, wait the minute out and
  hard-reload (Cmd-Shift-R / Ctrl-F5) before assuming the file is wrong.
- **A `theme.json` 404 in the browser console is normal until you upload one.** Every fresh
  install serves it, because there is no theme file until you make one. The widget treats a
  missing file as "no customisation" and renders the defaults. It is not an error to chase.
- **Your file survives deploys.** The deployment that ships `widget.js` is configured to
  leave both your `theme.json` and the `defaults/` folder alone, so a future release cannot
  delete your colours. This is pinned by a test
  (`test_the_theme_file_is_outside_the_prune_scope`), not just by care.
- **One bad value costs you that value and nothing else.** A colour it cannot read leaves the
  colour alone; a font keyword it does not know leaves the font alone. Only a file that is
  not valid JSON at all is ignored entirely.
- **It cannot break the widget.** Nothing in the file becomes code. The colour is matched
  against a hex pattern, the font is looked up in a fixed list, and the questions are
  rendered as plain text.

## The file

```json
{
  "highlightColor": "#1e4b8f",
  "fontFamily": "serif",
  "starterQuestions": {
    "en": ["What are the library hours?", "How do I book a study room?"],
    "es": ["¿Cuál es el horario de la biblioteca?"]
  }
}
```

Every key is optional. A file with only `highlightColor` in it changes only the colour.

**`highlightColor`** - a hex colour, six digits (`"#1e4b8f"`) or three (`"#a13"`). Colour
names and `rgb()` values are not read. You do not choose the text colour that sits on it: the
widget picks black or white, whichever is easier to read on your colour.

**`fontFamily`** - one of five words. No fonts are downloaded, so the list is limited to what
every Mac and Windows machine already has:

| Value | What you get |
|---|---|
| `"system"` | the visitor's own operating-system interface font (the default) |
| `"sans"` | Arial / Helvetica |
| `"serif"` | Georgia / Times New Roman |
| `"mono"` | a fixed-width font (Menlo / Consolas) |
| `"inherit"` | whatever font the page the widget sits on is already using |

Only the typeface changes. Sizes, weights and line spacing are not settable - see below.

**`starterQuestions`** - up to **four** per language, each up to **120 characters**. Extra or
over-long questions are dropped rather than shortened. `"es"` is optional: leave it out and
Spanish-speaking visitors see your English list, because nothing here is machine-translated.
Omit the whole block to keep the built-in questions in both languages.

---

## Why so few knobs

**One colour, not a palette.** A dozen colour fields is not a design system, it is a dozen
ways to produce an unreadable widget. The divider lines, the focus outline and its halo were
each measured against the specific surfaces they are drawn on to clear the WCAG 3:1
non-text-contrast requirement; exposing them would let a well-meaning edit undo that
silently. The one colour that is genuinely brand, and whose text can be derived safely, is
the one that is exposed.

**Text on the highlight is derived, not chosen.** Whatever colour you set, the widget puts
black or white on it - whichever contrasts more. The worst case that arithmetic can produce,
over every colour in the sRGB space, is 4.58:1, which is above the 4.5:1 that WCAG 1.4.3 asks
for. So there is no combination to get wrong and nothing for the widget to reject.

**Family only, never size or weight.** Text size, weight and line height are what keep the
panel readable when someone zooms to 200% or 400%; they are wired to the panel's layout
clamps. A font size field would turn a branding change into an accessibility regression that
nobody would notice until a student did.

**JSON, not JavaScript.** A `.js` config would be executable code running on the library's
own pages with the widget's privileges. A JSON file is data, and the widget allowlists every
value in it.

**No settings page.** A settings UI is a second surface to authenticate, host, and maintain
after the engagement ends. A file in a bucket needs none of that, and the bucket is already
yours.

## Accessibility

The widget's accessibility audit ([`accessibility-audit.md`](accessibility-audit.md)) measured
contrast against the **default** colours. Text drawn on the highlight is safe at any colour
you set, because it is derived rather than configured - but the highlight is also used as a
text colour on light surfaces (links in answers, source links, the starter-question chips),
and there the contrast depends on the colour you pick, so a custom highlight is the
customer's to verify. A dark, saturated brand colour like the shipped maroon is safe; a pale
or very bright one (yellows, light greens, pastels) will need checking with a contrast tool
against `#ffffff` and `#f1f3f6` before it goes live.

## For developers

- The widget-side code is the `THEME` section of `frontend/widget.js`; the downloadable copy
  of the defaults is `frontend/defaults/theme.json`, deployed to `defaults/theme.json` in the
  widget bucket by its own `BucketDeployment` (`prune=False`, prefix-scoped, and carrying the
  `Content-Disposition: attachment` that makes the output link a download rather than a page).
- The CDN behaviour (CORS, the 60-second TTL) and the prune exclusions are in the widget
  hosting section of `infra/infra/infra_stack.py`. Note the prune exclusion needs **two**
  patterns: `aws s3 sync --exclude` matches against the full path, so `theme.json` covers the
  root object only and `defaults/*` is what keeps the download itself from being deleted on
  every deploy.
- The fetch is deliberately blocking on mount, capped by `CONFIG.themeTimeoutMs` (1.5s), so a
  themed install never shows the default colours first and a dead CDN never withholds the
  widget for longer than that.
- Tests: the `theme.json` block at the end of `frontend/test/widget.contract.test.js`, and
  `test_the_theme_file_is_outside_the_prune_scope` /
  `test_the_theme_file_is_served_cross_origin_and_briefly_cached` in
  `infra/tests/unit/test_infra_stack.py`.
- The settings editor is `frontend/theme-editor.html`, served at the bucket root and
  linked by the `WidgetThemeEditor` output. It deliberately duplicates the widget's
  validation rules and the defaults file; the `theme-editor.html` block of the contract
  suite pins every copy against `widget.js` and `defaults/theme.json`, so a rule change
  in the widget fails tests until the editor matches.
- The editor's **Save** goes through `PUT /theme` on the stack's HTTP API, gated by the
  permanent theme-admin Cognito pool (email sign-in, managed login hosts every password
  flow) and served by `app/theme_handler.py`, whose IAM reaches exactly one object: the
  root `theme.json`. Librarian accounts are created with the printed
  `ThemeAdminCreateUserCommand` output (one `admin-create-user`, invitation by email);
  everything after that - first password, resets, change password, change email - is
  self-service from the editor page. The editor page itself is deploy-stamped
  (`Source.data`) with the save endpoint and sign-in configuration; the committed file
  carries four `__THEME_*__` placeholders and no URLs. Details and the full rationale
  live in CLAUDE.md's "Widget theme file" section.
