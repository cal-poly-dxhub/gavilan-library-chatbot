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

## Where the file goes

One file, named exactly `theme.json`, at the **top level of the widget bucket** - the same
place `widget.js` sits. The bucket name is generated per install, so read it off the stack
output rather than guessing:

```
aws cloudformation describe-stacks --stack-name GavilanChatbotStack \
  --query "Stacks[0].Outputs[?OutputKey=='WidgetThemeUpload'].OutputValue" --output text
```

That prints `s3://<the widget bucket>/theme.json`. `cdk deploy` prints the same thing at the
end of a deploy.

## Uploading it

1. In the same bucket you will upload to, find **`theme.example.json`** and download it. It
   is the annotated copy: every key, what it does, what the valid values are, and the
   defaults. It ships with the widget and is refreshed on every deploy.
2. Edit the values you want. Delete the `_readme` block if you like - the widget ignores it
   either way.
3. Save it as **`theme.json`** (not `theme.example.json` - that one is overwritten on the
   next deploy, so an edit to it is lost).
4. In the S3 console, open the widget bucket, choose **Upload**, add your `theme.json`, and
   upload. You do not need to set permissions, metadata, or anything else on the object.
5. Wait about a minute, then reload a page that has the widget on it. Your colour should be
   there on the first paint.

To go back to the shipped look, delete `theme.json`.

### A few things worth knowing

- **A `theme.json` 404 in the browser console is normal until you upload one.** Every fresh
  install serves it, because there is no theme file until you make one. The widget treats a
  missing file as "no customisation" and renders the defaults. It is not an error to chase.
- **About a minute, not instantly.** The CDN holds the file for 60 seconds, and so does the
  browser. If a change has not appeared, wait a minute and hard-reload before assuming it
  did not work.
- **Your file survives deploys.** The deployment that ships `widget.js` is configured to
  leave `theme.json` alone, so a future release cannot delete your colours. This is pinned by
  a test (`test_the_theme_file_is_outside_the_prune_scope`), not just by care.
- **One bad value costs you that value and nothing else.** A colour it cannot read leaves the
  colour alone; a font keyword it does not know leaves the font alone. The one exception is a
  file that is not valid JSON at all, which is ignored entirely - if a change is not showing
  up, paste the file into a JSON validator first. A trailing comma is the usual culprit.
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

- The widget-side code is the `THEME` section of `frontend/widget.js`; the shipped example is
  `frontend/theme.example.json`.
- The CDN behaviour (CORS, the 60-second TTL) and the prune exclusion are in the widget
  hosting section of `infra/infra/infra_stack.py`.
- The fetch is deliberately blocking on mount, capped by `CONFIG.themeTimeoutMs` (1.5s), so a
  themed install never shows the default colours first and a dead CDN never withholds the
  widget for longer than that.
- Tests: the `theme.json` block at the end of `frontend/test/widget.contract.test.js`, and
  `test_the_theme_file_is_outside_the_prune_scope` /
  `test_the_theme_file_is_served_cross_origin_and_briefly_cached` in
  `infra/tests/unit/test_infra_stack.py`.
