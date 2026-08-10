# Restyling the widget without a redeploy

**Who this is for:** developers working on the theming path. The customer-facing procedure
is NOT here - see [Where the customer's instructions live](#where-the-customers-instructions-live).

Three things about the chat widget can be changed without a deploy:

| What | Key | Default |
|---|---|---|
| The highlight colour (header, launcher, Send, your own chat bubbles, links) | `highlightColor` | `#8a1c30` |
| The typeface | `fontFamily` | `system` |
| The example questions offered before anyone types | `starterQuestions` | the four shipped ones, per language |

Everything else - sizes, spacing, the panel layout, the wording of the chrome - is fixed.
See [Why so few knobs](#why-so-few-knobs).

---

## Where the customer's instructions live

Not in this repo. The customer never has a checkout, so every instruction they need ships
with the thing it describes:

| Surface | Where it is | What it covers |
|---|---|---|
| The settings editor | `frontend/theme-editor.html`, at the widget bucket root | The whole workflow. Pick the colour, font and questions; **Save** publishes them to the live widget when a librarian is signed in, and the **Settings** panel holds the `theme.json` download and the link to the guide. |
| The guide | `frontend/theme-guide.html`, at the widget bucket root, linked only from that panel | The manual route: edit `theme.json` by hand and upload it in the S3 console, plus the troubleshooting (silent JSON failures, the 60-second cache, the normal 404, reverting). |
| The file's own `_readme` | inside `frontend/defaults/theme.json` | Every key, every font keyword and both caps, because a downloaded file travels alone. |

**One stack output points at any of this: `WidgetThemeEditor`.** The editor is the entry
point, and the guide and the download are reached from inside it. `WidgetThemeUpload` also
survives, because the guide's console procedure names it by output name, but it is a step
in a procedure rather than a starting point.

That is why the step-by-step is not duplicated here. Three copies of a procedure is two
copies to leave stale, and the two hosted ones are the copies the customer can actually
reach. Edit those files directly; the contract suite pins what they may contain.

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

**The settings page is static, and the file is still the contract.** This started as "no
settings page at all": a settings UI is a second surface to authenticate, host and maintain
after the engagement ends. What shipped keeps most of that saving. `theme-editor.html` is
one static object on the widget's own CDN with no build step, no server and no framework;
the only moving part behind it is one key-scoped Lambda on the API that already exists. The
runtime contract never changed - the widget still reads a `theme.json` from a bucket, so
the editor is a convenience over the file rather than a replacement for it, and deleting
the whole editor would leave the theming path working.

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
  `Content-Disposition: attachment` that makes a click on it save the file rather than render
  it in a tab). It has no stack output; the editor's Settings panel and the guide both link
  it.
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
- The settings editor is `frontend/theme-editor.html`, served at the bucket root and the
  ONE theming page an output links (`WidgetThemeEditor`). It deliberately duplicates the
  widget's validation rules and the defaults file; the `theme-editor.html` block of the
  contract suite pins every copy against `widget.js` and `defaults/theme.json`, so a rule
  change in the widget fails tests until the editor matches.
- `frontend/theme-guide.html` is the manual procedure, reachable ONLY from the editor's
  Settings panel. Nothing else in the product may link it: the whole point of dropping its
  output was that one entry point cannot go stale two ways. `test_the_theming_outputs_are_the_editor_and_the_upload_link`
  pins the output side, and the guide's block of the contract suite pins that it names no
  deleted output.
- Its main view is only the fields, the preview and Save. The download, the
  "Choose defaults" refill and the account controls sit in a native `<dialog>` behind the
  Settings control, which is also what supplies Escape-to-close and the focus return. The
  contract suite pins that split: the deleted upload copy stays deleted, those controls
  exist exactly once and only inside the panel, and an unsigned download is still
  `defaults/theme.json` byte for byte.
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
