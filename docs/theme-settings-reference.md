# `defaults/theme.json`, final text

The block below is the file, exactly as it should ship. The crew copies it
verbatim; nothing here is interpreted or reformatted. Everything after the
fence is our own note and is not part of the file.

Written as the customer's own settings file: they download it, edit it, and
upload it as `theme.json` at the bucket root. It is not framed as an example to
copy, because the copy on their laptop *is* the working file.

```json
{
  "_readme": [
    "This is the widget's settings file. The values below are the ones it ships with.",
    "Change what you want, save the file, and upload it to the same",
    "bucket widget.js sits in, named exactly theme.json. The widget reads it on every",
    "page load, so your change is live within about a minute.",
    "",
    "Deleting theme.json returns the widget to the values already filled in below.",
    "",
    "TWO THINGS THAT QUIETLY DO NOTHING. Uploading under any other name: it has to be",
    "theme.json. And editing the copy inside the defaults folder, which will do nothing. Your",
    "file belongs at the top level, next to widget.js.",
    "",
    "The keys the widget reads are listed below. Anything else in the file, including",
    "this _readme, is ignored, and any single key it cannot understand is ignored on its",
    "own: a typo in the colour leaves the colour alone rather than breaking the widget.",
    "The one exception is a file that is not valid JSON, which is ignored entirely. If the",
    "widget does not pick up your change, paste the file into a JSON validator.",
    "",
    "highlightColor: the one colour the widget is built from. It fills the header, the",
    "  launcher button, the Send button and your own chat bubbles, and tints the links.",
    "  A hex colour and nothing else: six digits (\"#8a1c30\") or three (\"#a13\").",
    "  Colour names (\"maroon\") and rgb() values are NOT read.",
    "",
    "fontFamily: one of these five words, and nothing else. Fonts are not downloaded, so",
    "  the choice is limited to what every Mac and Windows machine already has:",
    "    \"system\":  the visitor's own operating system interface font. This is the default.",
    "    \"sans\":    Arial or Helvetica.",
    "    \"serif\":   Georgia or Times New Roman.",
    "    \"mono\":    a fixed width font, Menlo or Consolas.",
    "    \"inherit\": whatever font the page the widget sits on is already using.",
    "  Only the typeface changes. Text sizes and spacing are fixed, because they are what",
    "  keep the panel readable when a visitor zooms in.",
    "",
    "starterQuestions: the example questions offered under the greeting, before anyone",
    "  has typed anything. Up to FOUR per language, each up to 120 characters; extra or",
    "  over-long ones are dropped. \"es\" is optional. Leave it out and Spanish visitors",
    "  see your English list, because nothing here is machine-translated. Remove the whole",
    "  starterQuestions block to keep the built-in questions in both languages."
  ],
  "highlightColor": "#8a1c30",
  "fontFamily": "system",
  "starterQuestions": {
    "en": [
      "What are the library hours?",
      "How do I check out a book?",
      "Where do I find my textbook?",
      "What research databases are available?"
    ],
    "es": [
      "¿Cuál es el horario de la biblioteca?",
      "¿Cómo pido prestado un libro?",
      "¿Dónde encuentro mi libro de texto?",
      "¿Qué bases de datos de investigación hay?"
    ]
  }
}
```

---

## Not part of the file: claims to confirm before it ships

Nine statements in that text describe widget behaviour nobody has checked
against the code. The crew confirms each and reports which are wrong; it does
not silently adjust the wording. If any is false we fix the copy here before it
ships, because a settings file that misleads a non-technical reader is worse
than one that says less.

1. The key names are exactly `highlightColor`, `fontFamily`, `starterQuestions`.
2. `#8a1c30` is genuinely the built-in default highlight colour.
3. The colour pattern accepts three-digit hex (`#a13`), not only six.
4. The accepted font values are exactly those five words.
5. The per-question limit really is 120 characters.
6. Over-long or surplus questions are dropped, not truncated, and do not reject
   the whole list.
7. Omitting `es` falls back to the English list, not to the built-in Spanish one.
8. Removing the whole `starterQuestions` block restores both built-in lists.
9. A `_readme` whose value is an array is ignored as cleanly as any other
   unknown key.

Known good already: the 60-second cache behind "live within about a minute", a
non-hex colour leaving the colour alone, and invalid JSON being ignored
entirely.
