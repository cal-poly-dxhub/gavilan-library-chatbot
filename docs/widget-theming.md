# Restyling the widget without a redeploy

For developers working on the theming path. The customer-facing instructions are not in this
repo - see [Where the customer's instructions live](#where-the-customers-instructions-live).

Three things about the chat widget change at runtime:

| What | Key | Default |
|---|---|---|
| The highlight colour (header, launcher, Send, the user's own bubbles, links) | `highlightColor` | `#8a1c30` |
| The typeface | `fontFamily` | `system` |
| The example questions offered before anyone types | `starterQuestions` | the four shipped ones, per language |

Everything else - sizes, spacing, layout, the wording of the chrome - is fixed. See
[Why so few knobs](#why-so-few-knobs).

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

Every key is optional. A file with only `highlightColor` changes only the colour.

**`highlightColor`** - a hex colour, six digits (`"#1e4b8f"`) or three (`"#a13"`). Colour names
and `rgb()` values are not read. The text colour on it is not yours to choose: the widget picks
black or white, whichever is easier to read on your colour.

**`fontFamily`** - one of five words. No fonts are downloaded, so the list is limited to what
every Mac and Windows machine already has: `system` (the visitor's own OS interface font, the
default), `sans` (Arial / Helvetica), `serif` (Georgia / Times New Roman), `mono` (Menlo /
Consolas), `inherit` (whatever the host page is already using). Typeface only - sizes, weights
and line spacing are not settable.

**`starterQuestions`** - up to four per language, each up to 120 characters. Extra or over-long
entries are dropped, not shortened. `"es"` is optional: leave it out and Spanish-speaking
visitors see your English list, because nothing here is machine-translated. Omit the whole block
to keep the built-in questions in both languages.

## Where the customer's instructions live

The customer never has a checkout, so every instruction ships with the thing it describes:

| Surface | Where | What it covers |
|---|---|---|
| The settings editor | `frontend/theme-editor.html`, at the widget bucket root | The whole workflow. Pick the colour, font and questions; **Save** publishes to the live widget for a signed-in librarian, and the **Settings** panel holds the `theme.json` download and the link to the guide. |
| The guide | `frontend/theme-guide.html`, same place, linked only from that panel | The manual route: edit `theme.json` by hand and upload it in the S3 console, plus the troubleshooting - silent JSON failures, the 60-second cache, the normal 404, reverting. |
| The file's own `_readme` | inside `frontend/defaults/theme.json` | Every key, every font keyword and both caps, because a downloaded file travels alone. |

`WidgetThemeEditor` is the only stack output framed as a starting point; the guide and the
download are reached from inside it. `WidgetThemeUpload` survives because the guide's console
procedure names it by output name, but it is a step in a procedure, not an entry point.

That is why the step-by-step is not duplicated here. Three copies of a procedure is two copies to
leave stale, and the two hosted ones are the copies the customer can actually reach. Edit those
files directly; the contract suite pins what they may contain.

## Why so few knobs

**One colour, not a palette.** A dozen colour fields is not a design system, it is a dozen ways
to produce an unreadable widget. The divider lines, the focus outline and its halo were each
measured against the specific surfaces they are drawn on to clear WCAG's 3:1 non-text-contrast
requirement; exposing them would let a well-meaning edit undo that silently. The one colour that
is genuinely brand, and whose text can be derived safely, is the one that is exposed.

**Text on the highlight is derived, not chosen.** Whatever you set, the widget puts black or
white on it, whichever contrasts more. The worst case that arithmetic can produce, over every
colour in sRGB, is 4.58:1 - above the 4.5:1 that 1.4.3 asks for. So there is no combination to
get wrong and nothing for the widget to reject.

**Family only, never size or weight.** Text size, weight and line height are what keep the panel
readable at 200% and 400% zoom; they are wired to the panel's layout clamps. A font-size field
would turn a branding change into an accessibility regression nobody notices until a student
does.

**JSON, not JavaScript.** A `.js` config would be executable code running on the library's own
pages with the widget's privileges. A JSON file is data, and the widget allowlists every value.

**The settings page is static, and the file is still the contract.** This started as "no settings
page at all" - a settings UI is a second surface to authenticate, host and maintain after the
engagement ends. What shipped keeps most of that saving: `theme-editor.html` is one static object
on the widget's own CDN with no build step, no server and no framework, and the only moving part
behind it is one key-scoped Lambda on an API that already exists. The runtime contract never
changed, so the editor is a convenience over the file rather than a replacement for it, and
deleting the whole editor would leave theming working.

## Accessibility

The [audit](accessibility-audit.md) measured contrast against the **default** colours. Text drawn
on the highlight is safe at any colour, because it is derived rather than configured - but the
highlight is also used as a text colour on light surfaces (links in answers, source links, the
starter-question chips), and there the contrast depends on the colour you pick. So a custom
highlight is the customer's to verify. A dark, saturated brand colour like the shipped maroon is
safe; a pale or very bright one - yellows, light greens, pastels - needs checking with a contrast
tool against `#ffffff` and `#f1f3f6` before it goes live.

## For developers

- The widget-side code is the `THEME` section of `frontend/widget.js`. The downloadable copy of
  the defaults is `frontend/defaults/theme.json`, deployed to `defaults/theme.json` by its own
  `BucketDeployment` (`prune=False`, prefix-scoped, `Content-Disposition: attachment` so a click
  saves it rather than rendering JSON in a tab). It has no stack output; the editor's Settings
  panel and the guide both link it.
- The CDN behaviour (CORS, the 60-second TTL) and the prune exclusions are in the widget hosting
  section of `infra/infra/infra_stack.py`. The prune exclusion needs **two** patterns:
  `aws s3 sync --exclude` matches the full path, so `theme.json` covers the root object only and
  `defaults/*` is what keeps the download from being deleted on every deploy.
- The theme fetch blocks the mount, capped by `CONFIG.themeTimeoutMs` (1.5s), so a themed install
  never shows the default colours first and a dead CDN never withholds the widget for longer.
- `frontend/theme-editor.html` is the settings editor, served at the bucket root and the one
  theming page an output links. Its main view is the fields, the preview and Save; the download,
  the "Choose defaults" refill, the link to the guide and the account controls sit in a native
  `<dialog>` behind the Settings control, which is what supplies Escape-to-close and focus return.
  It deliberately duplicates the widget's validation rules and the defaults file, and the contract
  suite pins every copy - so a rule change in the widget fails tests until the editor matches.
- `frontend/theme-guide.html` is the manual procedure, reachable **only** from that Settings
  panel. Nothing else may link it: the point of dropping its output was that one entry point
  cannot go stale two ways.
- Save goes through `PUT /theme`, gated by the theme-admin Cognito pool - the only sign-in in
  the product (email sign-in, managed
  login hosting every password flow) and served by `app/theme_handler.py`, whose IAM reaches
  exactly one object: the root `theme.json`. Accounts come from the `ThemeAdminCreateUserCommand`
  output; everything after that - first password, resets, change password, change email - is
  self-service from the editor. The page is deploy-stamped with the save endpoint and sign-in
  config, so the committed file carries four `__THEME_*__` placeholders and no URLs.
- Tests: the `theme.json` block at the end of `frontend/test/widget.contract.test.js`;
  `test_the_theme_file_is_outside_the_prune_scope`,
  `test_the_theme_file_is_served_cross_origin_and_briefly_cached` and
  `test_the_theming_outputs_are_the_editor_and_the_upload_link` in
  `infra/tests/unit/test_infra_stack.py`; and `test_theme_handler.py` for the save Lambda's copy
  of the rules.
