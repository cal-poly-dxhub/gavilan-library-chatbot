# Widget accessibility audit - WCAG 2.1 AA / Section 508

**What:** first accessibility assessment of the embeddable chat widget (`frontend/widget.js`) and the
shareable demo page (`frontend/demo-site.html`).
**Standard:** WCAG 2.1 Level AA, which is what Section 508 incorporates by reference (36 CFR 1194.1,
E205.4). Level A and AA criteria are in scope; AAA items are marked as such and are advisory only.
**Audited:** 2026-07-29, against commit `6c07630` on `fix/accessibility`.
**Status:** audit only. No code, config, infra, or test was changed by this document.
**Result:** 22 findings - 18 in the widget, 4 in the demo page. No blockers: 4 serious, 7 moderate,
11 minor. All text contrast already passes; the real gaps are screen-reader semantics and focus
containment.

---

## How to read this

Every finding is tagged with how it was established:

| Tag | Meaning |
|---|---|
| **[exercised]** | Reproduced in a real browser (Chrome 150 headless, driven over the DevTools Protocol so key presses go through the browser's own focus machinery). Includes computed styles, Chrome's accessibility tree, and geometry measured at four viewport sizes. |
| **[computed]** | A contrast ratio calculated from the colour values actually resolved by the browser, using the WCAG relative-luminance formula. Numbers are given, not impressions. |
| **[source]** | Read in the source only. Nothing about assistive-technology behaviour is asserted from a source read. |

**I did not run a screen reader.** No NVDA, JAWS, VoiceOver, TalkBack or Orca was involved. Where a
finding concerns what a screen reader would say, I report only the DOM and accessibility-tree facts I
could verify and label the announcement itself as unverified. Findings 2 and 3 are the two where that
distinction matters most; both are written as "here is the structural precondition for silence", not
"the screen reader is silent".

Two first-pass results were **discarded as measurement artifacts** rather than reported. They are
listed in "Method and limits" at the end, because a wrong finding is worse than a missing one.

---

## Top five, in fix order

Ordered so each fix is independent and the cheapest high-value work comes first. All five are inside
`frontend/widget.js`; none needs a dependency, a build step, or a change to the system prompt.

| # | Finding | SC | Severity | Size of fix |
|---|---|---|---|---|
| 1 | [F1](#f1) Message turns carry no speaker attribution - a screen reader cannot tell a student's question from the assistant's answer | 1.3.1 (A) | serious | ~6 lines; the `.sr-only` helper it needs already exists at `widget.js:908` and is currently unused |
| 2 | [F2](#f2) The "thinking" state's live region contains no text, and the slow-response note is revealed by un-hiding rather than by insertion | 4.1.3 (AA) | serious | ~8 lines in `showTyping()` |
| 3 | [F4](#f4) Focus is not contained in the open panel, and Escape only works while focus is already inside it | 2.4.3 (A) | serious | ~15 lines: move the Escape listener to the document, wrap focus at the panel edges |
| 4 | [F5](#f5) The launcher's and Send's focus rings are 1.77:1 against the page behind them, against a 3:1 requirement | 1.4.11 (AA) | serious | one colour value, twice (`widget.js:736`, `:895`) |
| 5 | [F14](#f14) The launcher's accessible name ("Open the library chat") does not contain its visible label ("Ask the Library"), so voice control cannot activate it by name | 2.5.3 (A) | moderate | one line (`widget.js:949`) |

Next two, if there is appetite: [F3](#f3) (the greeting and the four starter questions are never
presented to a screen-reader user when the panel opens) and [F7](#f7) (the error bubble's red styling
has never applied - a CSS specificity bug, verified in the browser).

---

## Already correct - please do not "fix" these

This list exists so a fix pass does not regress work that is already right.

**Keyboard and focus**
- The launcher is a real `<button type="button">` (`widget.js:946-948`). Enter **and** Space both open the panel. **[exercised]**
- Every interactive control is a real `<button>`, `<a>`, or `<textarea>`. There is no `div` with a click handler anywhere in the widget, so nothing is mouse-only. **[exercised]**
- Opening the panel moves focus into it; closing it returns focus to the launcher (`widget.js:1320`, `:1329-1331`). **[exercised]**
- Escape closes the panel when focus is inside it (`widget.js:1374-1379`), and `stopPropagation()` keeps it from also firing host-page handlers. **[exercised]**
- Nothing steals focus on page load. On both the harness page and the demo page, `document.activeElement` is `<body>` after load, and there is no `autofocus` anywhere. **[exercised]**
- Tab order inside the widget follows DOM order with no `tabindex` hacks - there is no positive `tabindex` in the file at all. **[exercised]**

**Live region** - this is the part most chat widgets get wrong, and it is already right:
- The thread is `role="log"` + `aria-live="polite"` + `aria-relevant="additions"` with `aria-atomic` left unset (`widget.js:992-995`). Chrome's accessibility tree confirms `live="polite" atomic=false relevant="additions"`. **[exercised]**
- That configuration means an arriving answer is announced as a **single addition** and the whole thread is **not** re-read on every update. The brief named re-announcing the thread as one of two failure modes to check for; it is already avoided by construction, and `aria-atomic` must stay unset for that to remain true.

**Semantics**
- The panel is `role="dialog"` with an accessible name (`widget.js:963-964`). Chrome computes it as `dialog "Gavilan College Library chat"`. **[exercised]**
- Every icon-only button has an accessible name, and every inline SVG is `aria-hidden="true"` + `focusable="false"` (`widget.js:631-632`), so no icon leaks into the accessibility tree. **[exercised]**
- `aria-expanded` on the sources disclosure is present and tracks state correctly in both directions (`widget.js:1052`, `:1063-1067`). **[exercised]**
- `aria-pressed` on the expand control tracks state (`widget.js:976`, `:1343`). **[exercised]**
- Send's accessible name ("Send message") contains its visible text ("Send"), which is what 2.5.3 requires. **[exercised]**
- Markdown tables render as real `<table>` / `<thead>` / `<th>` (`widget.js:534-537`), not a grid of divs. **[exercised]**
- Answers are built with `createElement` / `textContent` only, never `innerHTML` - pinned by the contract test.

**Contrast** - all **58** text pairs across the widget and the demo page pass 1.4.3. **[computed]**
Including the composer placeholder, which is left to the user-agent default and measures 4.61:1 in
Chrome 150.

**Reflow, zoom, motion**
- The panel clamps both dimensions to the viewport (`width: min(384px, calc(100vw - 32px))`, `height: min(560px, calc(100vh - 64px))`, `widget.js:744-745`). Measured fully inside the viewport with **no horizontal document scroll at 200%, 300% and 400% browser zoom** and at a 320px-wide viewport. 1.4.10 and 1.4.4 pass. **[exercised]**
- Both animations are gated on `prefers-reduced-motion: reduce` (`widget.js:808`, `:859`); under emulation the typing dots' `animation-name` and the caret's `transition-duration` both collapse. **[exercised]**
- Wide tables scroll inside their own `.md-table-wrap` container rather than bursting the bubble (`widget.js:836`), which is the 1.4.10-compliant way to handle a wide data table.

**No Shadow DOM ARIA trap** - see [item 5](#a5). This was worth checking and came back clean.

**Demo page** - `lang="en"`, exactly one `<h1>`, sound landmark structure (`main`, `nav` with
`aria-label`, `header`, `footer`, `aside`), `role="note"` on the demo banner, `<svg role="img"
aria-label="Open book">`, a real `<label for>` on all three sliders, and a cost disclosure with
correct `aria-expanded` + `aria-controls` whose IDs resolve. It reflows to 320px with no horizontal
scroll and does not interfere with the widget's focus behaviour. **[exercised]**

---

## Findings

Ranked by severity, then by cost to fix.

### Serious

<a id="f1"></a>
#### F1. A screen reader cannot tell a user turn from a bot turn

**Severity:** serious &nbsp;|&nbsp; **SC:** 1.3.1 Info and Relationships (Level A) &nbsp;|&nbsp; **[exercised]**

Speaker identity is carried entirely by CSS class: `msg msg--user` (`widget.js:1029`) versus
`msg msg--bot` (`widget.js:1109`), styled into right/left alignment and maroon/grey bubbles at
`widget.js:786-794`. The `.msg` wrappers carry no `role`, no `aria-label`, no
`aria-roledescription`, and no visually-hidden speaker text.

Chrome's accessibility tree for a four-turn conversation contains no node distinguishing the turns -
the messages appear as bare `StaticText`, so the programmatic relationship "this text was said by the
student, that text by the assistant" does not exist. Verified across seven messages: every one
returned `role: null, ariaLabel: null, anyVisuallyHiddenSpeakerLabel: false`.

Note the visual channel is not colour-alone (alignment also differs), so 1.4.1 Use of Color is not
implicated. This is purely a programmatic-structure failure.

**Smallest fix:** prepend one visually-hidden span to each message wrapper in `appendUserMessage`
and `appendBotMessage`, e.g. `You said:` / `Library assistant said:`. The `.sr-only` class this needs
is **already defined** at `widget.js:908-911` and is currently used by nothing - its own comment says
it is "for a11y live region labels", so this appears to be an intent that was never wired up.

<a id="f2"></a>
#### F2. The "thinking" state's live region has no text content

**Severity:** serious &nbsp;|&nbsp; **SC:** 4.1.3 Status Messages (Level AA) &nbsp;|&nbsp; **[exercised]** for the DOM facts, announcement itself unverified

Two separate structural problems in `showTyping()` (`widget.js:1132-1177`):

1. The indicator is `role="status"` with `aria-label="Assistant is typing"` (`widget.js:1140-1141`),
   but its **only children are three `aria-hidden="true"` dots**. Measured during a held request:
   `textContent` is `""`, length `0`, and `dotsAllAriaHidden: true`. A live region's announcement is
   built from its content; putting the message in `aria-label` instead of in the content is the
   standard way for a status region to end up with nothing to say. I could not confirm what any
   specific screen reader does here, so I am reporting the precondition, not the outcome.
2. The slow-response note ("Working…", `widget.js:1154-1158`) is **inserted into the DOM already
   hidden**, then revealed 6 seconds later by clearing `hidden`. The enclosing live region is
   `aria-relevant="additions"` (`widget.js:994`), which covers node *insertions*. Un-hiding a node
   that was inserted earlier is not an insertion. Measured: the node exists with `hidden: true` at
   insertion, `hidden: false` after the delay. It is also **not inside** the `role="status"` element
   (verified `hintIsInsideTheStatusNode: false`) - it is a sibling, so the status region does not
   cover it either.

The visual channel for "thinking" is three animated dots at 2.27:1 against the bubble (see [F6](#f6)),
so a user who cannot perceive them has no reliable substitute.

**Smallest fix:** put the status text inside the status element as an `.sr-only` child rather than in
`aria-label`, and create-and-append the "Working…" node when the timer fires instead of pre-inserting
it hidden.

<a id="f4"></a>
#### F4. Focus is not contained, and Escape is scoped to the panel

**Severity:** serious &nbsp;|&nbsp; **SC:** 2.4.3 Focus Order (Level A); ARIA APG dialog pattern &nbsp;|&nbsp; **[exercised]**

The panel is `role="dialog"` (`widget.js:963`) with no `aria-modal`, no focus containment, and an
Escape handler bound to the panel element (`widget.js:1374`) rather than the document. Measured
behaviour with the panel open:

- Tab from the Send button leaves the widget entirely and walks into the host page behind the panel. In the harness, 5 consecutive stops landed outside the widget; the panel stayed open the whole time.
- Shift+Tab from the panel's first control (Expand) goes straight into host-page content.
- **Escape with focus on a host-page control does nothing** - the panel stays open. Verified directly.
- To get from Send back to the panel's own Close button, a keyboard user must traverse the entire host page. On the demo page that is **20 focusable stops** with the cost panel closed (23 with it open, which adds three sliders and a disclosure); on the real `gavilan.edu/library` page it will be considerably more.

This is not an automatic failure: `role="dialog"` without `aria-modal` *declares* a non-modal dialog,
and a non-modal dialog is allowed to let focus out. But the widget currently takes the worst of both
readings - it declares a dialog, hides the launcher, and covers page content with a fixed-position
overlay, while leaving focus free to wander invisibly behind that overlay and scoping its only escape
hatch to the region focus has just left. Chrome reports the dialog as `modal=false`, so assistive
technology is told it is non-modal; the visual design says otherwise.

**Smallest fix,** in increasing order of commitment (either resolves the Escape half, which is the
worst part):
- Move the Escape listener from `panel` to the document, guarded on `state.open`. ~3 lines, fixes the trapped-open case on its own.
- Then either (a) contain focus: add `aria-modal="true"` and wrap Tab/Shift+Tab at the first and last focusable control in the panel's existing `keydown` handler (~12 lines, no new dependency); or (b) stay explicitly non-modal and add `aria-haspopup="dialog"` to the launcher so the relationship is at least announced.

Recommend (a). The panel behaves like a modal visually, so declaring and implementing it as one is
the smaller conceptual leap.

<a id="f5"></a>
#### F5. Focus-ring contrast on the launcher and Send

**Severity:** serious (launcher), moderate (the rest) &nbsp;|&nbsp; **SC:** 1.4.11 Non-text Contrast (Level AA) &nbsp;|&nbsp; **[computed]**

1.4.11 requires a focus indicator to reach 3:1 against adjacent colours. Because both rules use
`outline-offset: 2px`, the ring is drawn **on the page behind the control**, not on the control's own
fill - so the page background is the binding comparison, and it fails:

| Rule | Line | Ring colour | vs white host page | vs demo page `#f6f4f2` | vs the control's own fill | Verdict |
|---|---|---|---|---|---|---|
| `.launcher:focus-visible` | `:736` | `#9ec5ff` | **1.77:1** | **1.61:1** | 5.19:1 (not adjacent, offset 2px) | **fail** |
| `.composer__send:focus-visible` | `:895` | `#9ec5ff` | **1.77:1** (composer strip is `#fff`) | - | 5.19:1 (not adjacent) | **fail** |
| `.composer__input:focus-visible` outline | `:884` | brand @38% -> `#d3a9b0` | **2.09:1** | - | - | **fail** |
| `.composer__input:focus` border | `:886` | brand @55% -> `#bf828d` | 3.09:1 | - | - | pass, marginally |
| `.suggestion:focus-visible` | `:906` | brand @45% -> `#c897a0` | **2.42:1** (on thread `#fafbfc`) | - | - | **fail** |

The launcher is the worst of these in practice because it is the *only* widget control on the page
when the panel is closed - a keyboard user's entry point to the whole feature. The composer input is
the least bad: its outline fails at 2.09:1, but the simultaneous border change to `#bf828d` clears
3:1 at 3.09:1, so the focused state is still discernible. 2.4.7 Focus Visible (Level A) passes
throughout - a ring is always drawn; this is purely about its contrast.

**Smallest fix:** darken the ring. Computed candidates against the backgrounds it can land on:

| Candidate | white | demo `#f6f4f2` | thread `#fafbfc` |
|---|---|---|---|
| `#9ec5ff` (current) | 1.77:1 | 1.61:1 | 1.71:1 |
| `#3b82f6` | 3.68:1 | 3.35:1 | 3.55:1 |
| `#2563eb` | **5.17:1** | **4.71:1** | **4.99:1** |

`#2563eb` clears 3:1 comfortably on any light background. One caveat worth stating plainly: the
widget embeds into a page whose background it cannot know, so **no single flat ring colour is robust
on every host**. A dark host page would invert this problem. The durable answer is a two-tone ring
(light inner, dark outer) the way Chrome's own `outline: auto` works; the flat darker blue is the
minimal fix for the light-background reality of `gavilan.edu`.

Note for whoever does this: `frontend/test/widget.contract.test.js:1136-1144` **pins** the softened
composer ring as a deliberate design choice ("a translucent tint of the brand, not full-strength"),
so changing `:884` requires updating that test in the same commit. The launcher and Send rings at
`:736` / `:895` are not pinned by any test.

### Moderate

<a id="f3"></a>
#### F3. The greeting and the four starter questions are never presented on open

**Severity:** moderate &nbsp;|&nbsp; **SC:** 2.4.3 Focus Order (Level A); ARIA APG dialog practice &nbsp;|&nbsp; **[exercised]**

`mount()` seeds the greeting and the suggestion chips into the thread at load time
(`widget.js:1382-1383`), while `panel.hidden` is still `true`. Verified at that moment:
`panelHiddenNow: true`, `messagesAlreadyInThread: 1`, `suggestionChips: 4`.

So when the panel opens, no node is added to the live region - there is nothing for it to announce -
and `openPanel()` sends focus straight to the composer textarea (`widget.js:1320`, verified). A
screen-reader or keyboard user therefore lands mid-dialog on an empty text box, with the greeting,
the four starter questions, and the Close button all *behind* focus in the tab order. Everything is
reachable by browsing or Shift+Tab, so nothing is strictly inaccessible; the content is just never
offered.

I want to be precise about the mapping: this does not cleanly fail a single success criterion. It is
a Focus Order concern (focus lands somewhere that does not preserve the dialog's meaning) and a clear
departure from the ARIA APG dialog pattern, which is not itself normative. I am rating it moderate on
that basis rather than claiming a hard AA violation.

**Smallest fix:** on open, move focus to the panel container (`tabindex="-1"` + `focus()`) instead of
the textarea, so the dialog's name and contents are what the user encounters first. Alternatively
keep focus on the textarea and give it an `aria-describedby` pointing at the greeting - both ends of
that reference are inside the shadow root, so it resolves (see [item 5](#a5)).

<a id="f6"></a>
#### F6. Non-text contrast on control boundaries and the typing indicator

**Severity:** moderate (input border, typing dots), minor (section and table rules) &nbsp;|&nbsp; **SC:** 1.4.11 Non-text Contrast (Level AA) &nbsp;|&nbsp; **[computed]**

| What | Line | Colour | Against | Ratio | Need | Verdict |
|---|---|---|---|---|---|---|
| `.typing .dot` - the status indicator itself | `:855` | `#9aa4b0` | bot bubble `#f1f3f6` | **2.27:1** | 3:1 | **fail** |
| `.composer__input` border - the text field's boundary | `:877` | `#c8cfd8` | `#ffffff` | **1.57:1** | 3:1 | **fail** |
| `.suggestion` chip border | `:901` | `#d9dee5` | thread `#fafbfc` | **1.31:1** | 3:1 | **fail** |
| `.md-table` cell borders | `:842` | `#dfe3e8` | bot bubble | **1.16:1** | 3:1 | fail (see note) |
| `.sources` top rule | `:797` | `#dfe3e8` | bot bubble | **1.16:1** | 3:1 | fail (see note) |
| `.panel` border | `:746` | `#d9dee5` | white page | **1.35:1** | 3:1 | fail (see note) |

The first three are the ones that matter. The typing dots are the sole visual carrier of the
"thinking" state, which compounds [F2](#f2). The composer border is the only thing delimiting the
text field - though 1.4.11's Understanding document allows a boundary to fall below 3:1 when the
component is discernible without it, and the placeholder text inside does make the field visible, so
this is arguable rather than clear-cut.

The last three are container edges and table rules. 1.4.11 does not apply to purely decorative
boundaries, and the panel additionally carries a strong drop shadow (`widget.js:748`) that does the
separating work. I list them for completeness and would not spend effort on them.

**Smallest fix:** darken the three that matter to roughly `#8b96a3` or beyond (which clears 3:1 on
both the bubble and white). Leave the decorative rules alone.

<a id="f7"></a>
#### F7. The error bubble's styling has never applied (CSS specificity)

**Severity:** moderate &nbsp;|&nbsp; **SC:** none failed (see below) &nbsp;|&nbsp; **[exercised]**

`.bubble--error` (`widget.js:795`) sets the red error palette, but it is a single-class selector
(specificity 0,1,0) competing with `.msg--bot .bubble` (`widget.js:794`, specificity 0,2,0). The more
specific rule wins regardless of source order, so the error styling is dead code.

Verified by forcing a network failure and reading computed styles: the error bubble renders
`background: rgb(241,243,246)` / `color: rgb(26,29,33)` - **byte-identical to a normal bot bubble** -
while the `--error-bg: #fdecea` and `--error-ink: #8a1c12` tokens sit defined and unused. A failed
send is visually indistinguishable from a successful answer apart from the words in it.

To be clear about the standard: this is **not** a WCAG failure. 3.3.1 Error Identification (Level A)
is satisfied, because the error is described in text (`widget.js:1185-1187`), and 1.4.1 Use of Color
is satisfied precisely because there is no colour distinction to depend on. It is a real defect that
an accessibility pass is the natural place to catch, and fixing it *improves* the error state's
perceivability rather than fixing a violation. Both the intended and the actual palettes pass 1.4.3
anyway (8.14:1 intended, 15.21:1 as rendered), so there is no contrast risk either way.

**Smallest fix:** raise the selector to `.msg--bot .bubble.bubble--error`.

<a id="f9"></a>
#### F9. No headings anywhere in the widget; markdown headings render as paragraphs

**Severity:** moderate &nbsp;|&nbsp; **SC:** 1.3.1 Info and Relationships (Level A) &nbsp;|&nbsp; **[exercised]**

`renderMarkdown` turns a markdown heading into `<p class="md-heading">` with `font-weight: 700`
(`widget.js:581-582`, `:821`) - visually a heading, programmatically a paragraph. The panel's own
title is a `<span>` (`widget.js:969-971`). Verified against a rendered answer containing
`## Fall hours`: `realHeadingElementsInWidget: 0`, `markdownHeadingRenderedAs: "P (class md-heading)"`.

The model does produce structured answers (the hours answer is a table, and multi-section answers use
headings), so a user who navigates by heading gets nothing inside the panel.

**Smallest fix:** in the heading branch, emit `h3`-`h6` derived from the `#` count instead of `p`, or
add `role="heading"` + `aria-level`. Keep the `.md-heading` class so the styling is unchanged.

<a id="f14"></a>
#### F14. The launcher's accessible name does not contain its visible label

**Severity:** moderate &nbsp;|&nbsp; **SC:** 2.5.3 Label in Name (Level A) &nbsp;|&nbsp; **[exercised]**

The launcher shows the text "Ask the Library" (`widget.js:54`, rendered at `:955`) but its
`aria-label` is "Open the library chat" (`widget.js:949`), which overrides the visible text as the
accessible name. 2.5.3 requires the accessible name to *contain* the visible label text. It does not,
so a speech-input user who says "click Ask the Library" - reading the words on screen - will not
activate the button.

This is a Level A criterion and a one-line fix, which is why it is in the top five despite the
moderate severity rating.

**Smallest fix:** either drop the `aria-label` (the button's own text is a perfectly good name) or
make it include the visible text, e.g. `"Ask the Library - open the library chat"`.

The other icon-only controls are fine: Expand and Close have no visible text label, so 2.5.3 does not
apply, and Send's "Send message" correctly contains "Send".

<a id="f16"></a>
#### F16. After a failed send, "Try again" sits behind focus

**Severity:** moderate &nbsp;|&nbsp; **SC:** 2.4.3 Focus Order (Level A) &nbsp;|&nbsp; **[exercised]**

`deliver()`'s error path calls `focusInput()` (`widget.js:1296`), putting focus in the composer. The
retry button lives inside the thread (`widget.js:1190-1199`), which is *earlier* in DOM order, so
Tab never reaches it - measured: Tab from the textarea goes to Send, then straight out to the host
page. It is reachable only by Shift+Tab, or by cycling through the whole host page.

The button is a real `<button>` with `tabIndex: 0` and is fully operable once found; this is about
discoverability, not reachability.

**Smallest fix:** move focus to the retry button when an error is rendered, rather than to the
composer.

<a id="d1"></a>
#### D1. Demo page: two `<h2>` elements precede the only `<h1>`

**Severity:** moderate &nbsp;|&nbsp; **SC:** 1.3.1 (Level A), 2.4.6 Headings and Labels (Level AA) &nbsp;|&nbsp; **[exercised]**

Measured heading sequence on `demo-site.html`:

```
H2: "This conversation Estimate"      <- cost panel, demo-site.html:387
H2: "Monthly estimate Estimate"       <- cost panel, demo-site.html:405
H1: "Gavilan College Library"         <- demo-site.html:514
H2: "Ask the library assistant"  ...  <- and five more H2s
```

`firstHeadingIsH1: false`. The cost panel sits above the masthead in DOM order (deliberately - it is
demo scaffolding attached to the banner), so its two headings come before the page's actual `<h1>`.
The outline reads as though the page begins mid-document.

**Smallest fix:** demote the two cost-panel headings to a non-heading element with the same styling,
or promote them to `<h2>` *after* the `<h1>`. Given the cost panel is deliberately not part of the
library chrome, making them non-headings is the more honest structure.

### Minor

<a id="f8"></a>
#### F8. The widget never declares its own language

**Severity:** minor today; **blocking for the planned Spanish support** &nbsp;|&nbsp; **SC:** 3.1.2 Language of Parts (Level AA) &nbsp;|&nbsp; **[exercised]**

There is no `lang` attribute anywhere in `widget.js` - verified `anyLangInsideShadow: 0`. The shadow
content inherits `en` from the host document's `<html lang="en">` (verified `hostClosestLang: "en"`),
so 3.1.1 Language of Page is satisfied by the host and nothing is wrong **today, in English**.

Two forward-looking problems, both worth recording now because the brief says Spanish is being
considered:

1. The model can already answer in Spanish - a student who asks in Spanish will often get a Spanish reply - and that reply is currently emitted inside an `en` subtree with no `lang="es"`. A screen reader will read Spanish text with an English voice and pronunciation rules. That is a live 3.1.2 exposure right now, not a hypothetical, and it cannot be fixed in the widget alone: the widget does not know what language the answer is in. Either the backend reports it on the response, or the widget guesses, and guessing is worse.
2. `:host { all: initial }` (`widget.js:706`) resets `direction` to `ltr`, so the widget would override a right-to-left host page. Irrelevant for Spanish (also LTR); noted for completeness.

**Smallest fix now:** set `root.lang = "en"` so the widget's own chrome declares its language rather
than inheriting it. **For bilingual support:** add a language field to the `/query` response contract
and set `lang` per answer bubble. That is a contract change, not a widget change, and belongs in the
Spanish-support design rather than in an accessibility fix pass.

<a id="f10"></a>
#### F10. Table headers have no `scope`, tables have no caption

**Severity:** minor &nbsp;|&nbsp; **SC:** 1.3.1 (Level A) &nbsp;|&nbsp; **[exercised]**

`appendTableRow` creates `th` elements with no `scope` attribute (`widget.js:512-518`). Verified on a
rendered hours table: `thScopeAttrs: [null, null]`, `hasCaption: false`. A single header row inside
`<thead>` is generally inferred correctly by assistive technology, which is why this is minor rather
than moderate.

**Smallest fix:** set `scope="col"` when `tag === "th"`. One line.

<a id="f11"></a>
#### F11. Sources disclosure has no `aria-controls`

**Severity:** minor &nbsp;|&nbsp; **SC:** none failed; ARIA APG recommendation &nbsp;|&nbsp; **[exercised]**

The toggle exposes `aria-expanded` correctly but has no `aria-controls`, and the list it controls has
no `id` (verified `ariaControls: null`, `listHasId: null`). `aria-controls` is a recommendation, not a
requirement, and 4.1.2 is satisfied by `aria-expanded` alone - so nothing fails. Listed because it is
the natural companion fix and because it is a good illustration of [item 5](#a5): the id would need to
be unique per message, and both ends would sit inside the shadow root, so the reference resolves.

<a id="f12"></a>
#### F12. `maxlength` silently stops typing with no feedback

**Severity:** minor &nbsp;|&nbsp; **SC:** 3.3.2 Labels or Instructions (Level A), arguable &nbsp;|&nbsp; **[exercised]**

The composer sets `maxlength="1000"` (`widget.js:1004`) and its own code comment says this exists "so
a user gets feedback instead of typing a wall of text". There is no feedback: verified no counter
element, no `aria-describedby`, no live region. At 1000 characters the keyboard simply stops
producing text, which is disorienting and disproportionately so for screen-reader and
cognitive-disability users. Nothing is submitted in error, so no criterion is clearly failed.

**Smallest fix:** an `.sr-only` + visible counter that becomes assertive near the limit, associated
via `aria-describedby` (same shadow root, so it resolves).

<a id="f13"></a>
#### F13. The launcher does not announce that it opens a dialog

**Severity:** minor &nbsp;|&nbsp; **SC:** 4.1.2 arguably satisfied &nbsp;|&nbsp; **[exercised]**

Verified `aria-expanded: null`, `aria-haspopup: null`, `aria-controls: null` on the launcher. Because
the launcher is hidden while the panel is open (`widget.js:1319`), there is no persistent expanded
state to expose, so `aria-expanded` would arguably be wrong here and 4.1.2 is satisfied. What is
missing is any hint that activating it opens a dialog rather than navigating.

**Smallest fix:** `aria-haspopup="dialog"`. Pairs naturally with [F4](#f4) option (b).

<a id="f15"></a>
#### F15. Composer has no persistent visible label

**Severity:** minor &nbsp;|&nbsp; **SC:** 3.3.2 (Level A), satisfied-but-fragile &nbsp;|&nbsp; **[exercised]**

The textarea is labelled by `aria-label="Type your question"` (`widget.js:1005`) plus a placeholder
"Ask a question…" (`:1006`). There is no `<label>` element anywhere in the widget (verified
`hasVisibleLabelElement: 0`). The accessible name is solid, so 4.1.2 passes; the visible cue is the
placeholder, which disappears as soon as the user types. For a chat composer sitting next to a Send
button the purpose stays obvious from context, so I rate this minor and would not necessarily change
it - recorded because "placeholder as the only visible label" is the single most common finding in
external audits and it is better to have a stated position on it than to be asked cold.

<a id="f17"></a>
#### F17. `.retry` is the one control with no widget-defined focus style

**Severity:** minor &nbsp;|&nbsp; **SC:** 2.4.7 Focus Visible (Level A) - **passes** &nbsp;|&nbsp; **[exercised]**

The widget defines nine `:focus-visible` rules but none for `.retry` (`widget.js:864-869`). Verified:
the button falls back to Chrome's default ring, `outline: auto 1px rgb(0,95,204)`, which measures
5.38:1 against the bubble and is clearly visible - so 2.4.7 passes and this is a consistency point,
not a defect. Other browsers' defaults differ.

<a id="f18"></a>
#### F18. Box dimensions and font sizes are locked in `px`

**Severity:** minor &nbsp;|&nbsp; **SC:** 1.4.4 Resize Text (Level AA) - **passes** &nbsp;|&nbsp; **[exercised]**

`.root` sets `font-size: 15px` (`widget.js:719`) and several boxes are fixed: `.composer__send
{ height: 40px }` (`:891`), `.composer__input { min-height: 40px; max-height: 120px }` (`:876`).

1.4.4 **passes**, and I want to be exact about why rather than flag this as a violation: browser zoom
scales `px`, so at 200% everything scales proportionally and nothing clips - measured, no clipping at
200%, 300% or 400%. The residual risk is narrower: a user who raises only their *default font size*
(rather than zooming) gets no change from the widget, because `:host { all: initial }` plus an
absolute `font-size` breaks the inheritance chain that setting would normally travel down. Using
`rem` would honour it, since `rem` resolves against the document root and is not affected by
`all: initial` on the host.

**Smallest fix:** express `.root`'s `font-size` in `rem` and the two box dimensions in `em`. Low
priority; no criterion is failed.

<a id="d2"></a>
#### D2. Demo page: no skip link

**Severity:** minor (advisory) &nbsp;|&nbsp; **SC:** 2.4.1 Bypass Blocks (Level A), arguably N/A &nbsp;|&nbsp; **[exercised]**

Verified by walking the real tab order: no in-page anchor and `main` has no `id`. Twelve tab stops
(the banner link, the cost control, a 3-link utility strip and a 7-item nav) come before the first
main-content link, and the widget launcher is the last stop on the page, 21st. 2.4.1
addresses blocks repeated *across* pages, and this is a single standalone demo page, so it is
arguably not applicable. Noted because the demo is what a client will actually click, and because it
would become a genuine 2.4.1 issue if the demo ever grew a second page.

<a id="d3"></a>
#### D3. Demo page: slider read-outs are not announced

**Severity:** minor &nbsp;|&nbsp; **SC:** 4.1.2 (Level A), partially &nbsp;|&nbsp; **[exercised]**

All three range inputs are correctly labelled with `<label for>` (`demo-site.html:409`, `:416`,
`:423`) - that part is right. But the formatted read-outs (`#v-users`, `#v-q`, `#v-len`) and the
explanatory `#len-hint` are plain spans with no `aria-live` and no association to the input (verified
all four return `ariaLive: null, role: null`). The slider announces its raw value, so "3" is
conveyed but "3 questions" and the hint sentence explaining what the setting costs are not.

**Smallest fix:** `aria-valuetext` on each input, updated in the existing `renderEstimate()`.

<a id="d4"></a>
#### D4. Demo page: smooth scroll is not gated on `prefers-reduced-motion`

**Severity:** minor (advisory, AAA) &nbsp;|&nbsp; **SC:** 2.3.3 Animation from Interactions (Level **AAA**) &nbsp;|&nbsp; **[exercised]**

`demo-site.html:841` calls `panel.scrollIntoView({ block: "nearest", behavior: "smooth" })` when the
cost panel opens, with no reduced-motion check. 2.3.3 is Level AAA and therefore **outside** the AA
target - recorded only so the list is complete. Worth contrasting with the widget itself, which does
gate both of its animations correctly.

---

## The ten areas, answered

<a id="a1"></a>
### 1. Keyboard reachability

**Clean.** The launcher is a real `<button type="button">` (`widget.js:946-948`); Enter and Space both
open the panel (both exercised). Every control is a native interactive element - there is no
`div`-with-click-handler in the file - so nothing is mouse-only. Measured full cycle with the panel
open:

```
[host page stops...] -> Expand -> Close -> suggestion x4 -> textarea -> Send -> [wraps]
```

Send, Close, Expand, all four suggested-prompt chips, the sources disclosure, the source links, and
the retry button are all reachable by Tab alone and all activate from the keyboard. Order follows DOM
order; there is no `tabindex` in the file. Two caveats that are focus-*order* issues rather than
reachability ones: [F4](#f4) (the cycle runs through the whole host page) and [F16](#f16) (retry sits
behind focus). While a request is pending, Send is correctly disabled and drops out of the tab order.

<a id="a2"></a>
### 2. Focus management

Open moves focus into the panel (textarea); close returns it to the launcher; Escape closes from
inside; nothing steals focus on load. All four exercised and correct. The gaps: no containment and
Escape is scoped to the panel ([F4](#f4)), and focus lands mid-dialog past the greeting and the
starter questions ([F3](#f3)).

<a id="a3"></a>
### 3. Semantics and ARIA

The panel is `role="dialog"` with an accessible name; Chrome computes `dialog "Gavilan College Library
chat"`, `modal=false`. Icon-only buttons all have names and every SVG is `aria-hidden` +
`focusable="false"`. The sources disclosure exposes `aria-expanded` with correct state in both
directions; `aria-controls` is absent but not required ([F11](#f11)). The one real failure is the
message thread: turns are distinguished only by CSS class, with nothing in the accessibility tree
telling a student's question from the assistant's answer ([F1](#f1)). Also: no headings anywhere
([F9](#f9)) and the launcher's name does not contain its visible label ([F14](#f14)).

<a id="a4"></a>
### 4. Live region

Both failure modes named in the brief were checked separately, and they land differently:

- **Re-announcing the whole thread: already avoided.** `role="log"` + `aria-live="polite"` + `aria-relevant="additions"` with `aria-atomic` unset means each arriving answer is a single addition, not a re-read of the conversation. Confirmed in Chrome's accessibility tree (`live="polite" atomic=false relevant="additions"`). This is correct and `aria-atomic` must stay unset.
- **Silence: a real risk, for the "thinking" state only.** An arriving *answer* is a genuine node insertion into the live region, so the announcement path exists. The *pending* state is where it breaks: the `role="status"` element's text content is empty (measured length 0, all dots `aria-hidden`), its message lives in `aria-label`, and the "Working…" note is revealed by un-hiding a pre-inserted node rather than by insertion - which `aria-relevant="additions"` does not cover ([F2](#f2)).

I did not run a screen reader, so I am reporting the structural preconditions rather than asserting
what is or is not spoken.

<a id="a5"></a>
### 5. The Shadow DOM trap

**Clean - nothing in this widget relies on a cross-boundary ID reference.** I searched
`widget.js` for every ID-consuming attribute (`aria-labelledby`, `aria-controls`, `aria-describedby`,
`aria-details`, `aria-owns`, `aria-flowto`, `for`) and for every `id` assignment. Results: **zero**
ID-based ARIA references. The only two `id` writes are `link.id = FONT_LINK_ID` (`widget.js:694`, the
Google Fonts `<link>` in the host head) and `host.id = HOST_ID` (`:930`, the shadow host element).
Neither is an ARIA reference. Every accessible name in the widget comes from `aria-label` or from
element content, both of which work identically inside a shadow root.

So there is nothing here failing silently. That appears to be by construction rather than luck -
`aria-label` is used consistently at all eight elements that need a name (launcher, panel, expand,
close, thread, textarea, send, typing indicator).

The demo page does use ID references - `aria-controls="cost-panel"` and `aria-labelledby="cost-toggle"`
(`demo-site.html:362`, `:382`) and `for="s-users"` etc. - and all of them are **host-document to
host-document**, never crossing into the shadow tree. Verified live: `controlsTargetExists: true`,
`labelledbyTargetExists: true`, and the label resolves to the button's own text "What does this cost?".

**The constraint to carry forward,** since several fixes above suggest adding such references: an
ID reference works fine when **both ends are inside the same shadow root**. `aria-describedby` from
the textarea to a greeting or a character counter, `aria-controls` from the sources toggle to its
list, `aria-labelledby` from the panel to a title element - all safe, all resolve. The trap fires
only if something inside the shadow root points at an `id` in the host page (or vice versa), which
would fail silently with no console error. Nothing does that today.

<a id="a6"></a>
### 6. Contrast

84 pairs computed from the values the browser actually resolves (including alpha compositing and
`color-mix`), plus the user-agent placeholder colour measured in situ. Full numbers are in the tables
under [F5](#f5) and [F6](#f6); the summary:

| Category | Checked | Pass | Fail |
|---|---|---|---|
| Widget text (1.4.3, 4.5:1) | 18 | **18** | 0 |
| Demo page text (1.4.3) | 40 | **40** | 0 |
| Widget focus indicators (1.4.11, 3:1) | 10 | 6 | **4** |
| Widget UI boundaries (1.4.11, 3:1) | 7 | 0 | **7** (3 that matter, 4 decorative) |
| Demo page UI (1.4.11, 3:1) | 9 | 5 | 4 (all decorative or on a disabled control) |

Requested specifics:

| Element | Colours | Ratio | Requirement | Verdict |
|---|---|---|---|---|
| Body text, bot bubble | `#1a1d21` on `#f1f3f6` | **15.21:1** | 4.5:1 | pass |
| Body text, user bubble | `#ffffff` on `#8a1c30` | **9.17:1** | 4.5:1 | pass |
| Placeholder (UA default, measured) | `#757575` on `#ffffff` | **4.61:1** | 4.5:1 | pass |
| Metadata - source excerpts | `#5b6570` on `#f1f3f6` | **5.34:1** | 4.5:1 | pass |
| Metadata - "Sources (n)" toggle, 11px bold | `#5b6570` on `#f1f3f6` | **5.34:1** | 4.5:1 | pass |
| Metadata - "Try asking:" label | `#5b6570` on `#fafbfc` | **5.73:1** | 4.5:1 | pass |
| Metadata - "Working…" hint | `#5b6570` on `#f1f3f6` | **5.34:1** | 4.5:1 | pass |
| Links in answers (`.md-link`) | `#8a1c30` on `#f1f3f6` | **8.25:1** | 4.5:1 | pass |
| Source links | `#8a1c30` on `#f1f3f6` | **8.25:1** | 4.5:1 | pass |
| Launcher icon + label | `#ffffff` on `#8a1c30` | **9.17:1** | 4.5:1 (text) / 3:1 (icon) | pass |
| Launcher **focus ring** | `#9ec5ff` on the page behind | **1.77:1** | 3:1 | **fail** |
| Typing dots | `#9aa4b0` on `#f1f3f6` | **2.27:1** | 3:1 | **fail** |
| Composer border | `#c8cfd8` on `#ffffff` | **1.57:1** | 3:1 | **fail** |

There are no timestamps in the widget - no message carries a time. Worth knowing before anyone adds
them, since `--muted` at 12px is where that text would land (5.34:1, which would pass).

One correction the browser forced: the error bubble's *intended* palette (`#8a1c12` on `#fdecea`,
8.14:1) never renders. What actually renders is `#1a1d21` on `#f1f3f6` at 15.21:1. Both pass; see
[F7](#f7).

<a id="a7"></a>
### 7. Reflow, zoom and motion

**All three pass.** Measured at four viewport sizes and under reduced-motion emulation.

| Condition | Panel geometry | Fully in viewport | Horizontal document scroll |
|---|---|---|---|
| 1280x1024 (100%) | 384x560 | yes | no |
| 640x512 (= 200% zoom) | 384x448 | yes | no |
| 426x341 (= 300% zoom) | 384x277 | yes | no |
| 320x256 (= 400% zoom, the 1.4.10 test) | 288x192 | yes | no |
| 320x640 (narrow phone) | 288x560 | yes | no |

1.4.10 Reflow passes: no two-dimensional scrolling at 320 CSS px, and wide tables scroll inside their
own container, which the criterion explicitly permits for data tables. 1.4.4 Resize Text passes: the
panel clamps both dimensions with `min(..., calc(100vh - 64px))`, so it shrinks with the viewport
instead of overflowing it.

The honest caveat is usability rather than conformance: at the 400% test size the conversation area
is **79px tall** - about one suggestion chip, as the screenshot I captured confirms - with the header
and composer taking the rest. Everything is reachable by scrolling, so it conforms, but it is a
cramped experience worth knowing about before a client tries it at high zoom.

No dimension or font size clips text under zoom ([F18](#f18) explains why `px` is not a 1.4.4
problem here, and what the narrower residual risk actually is).

Motion: both of the widget's animations are gated on `prefers-reduced-motion: reduce`
(`widget.js:808`, `:859`) and both collapse correctly under emulation. The only ungated motion in the
project is the demo page's smooth `scrollIntoView` ([D4](#d4)), which is a AAA item.

<a id="a8"></a>
### 8. Language

The widget sets no `lang` and inherits `en` from the host page ([F8](#f8)). 3.1.1 is satisfied by the
host; the exposure is 3.1.2 once answers are not English - which can already happen today, because
the model will answer a Spanish question in Spanish.

**Complete inventory of user-visible English strings in the widget - 26 strings.** This is the input
list for the Spanish decision. Seven live in the `CONFIG` object and are trivially externalisable;
the other nineteen are hardcoded at their point of use and each needs touching.

*In `CONFIG` (`widget.js:43-67`) - 7:*

| Line | String | Where it shows |
|---|---|---|
| `:53` | `Library Help` | panel header title |
| `:54` | `Ask the Library` | launcher button label |
| `:55-58` | `Hi! I'm the Gavilan College Library assistant. I can help with hours, checking out materials, textbooks, and what the library offers. What can I help you find?` | seeded greeting |
| `:62` | `What are the library hours?` | starter chip 1 |
| `:63` | `How do I check out a book?` | starter chip 2 |
| `:64` | `Where do I find my textbook?` | starter chip 3 |
| `:65` | `What research databases are available?` | starter chip 4 |

*Hardcoded at the use site - 19:*

| Line | String | Kind |
|---|---|---|
| `:151-153` | `The library assistant isn't connected yet. Please try again later.` | fallback answer when `data-api-url` is unset |
| `:949` | `Open the library chat` | launcher `aria-label` |
| `:964` | `Gavilan College Library chat` | panel `aria-label` (the dialog's accessible name) |
| `:975` | `Expand chat` | expand `aria-label`, initial |
| `:981` | `Close chat` | close `aria-label` |
| `:982` | `×` | close button glyph |
| `:995` | `Conversation` | thread `aria-label` |
| `:1005` | `Type your question` | textarea `aria-label` |
| `:1006` | `Ask a question…` | textarea `placeholder` |
| `:1010` | `Send` | send button text |
| `:1011` | `Send message` | send `aria-label` |
| `:1060` | `Sources (` + n + `)` | disclosure label, plural form |
| `:1060` | `Source` | disclosure label, singular form |
| `:1120` | `Sorry, I didn't get a response. Please try again.` | empty-answer fallback |
| `:1140` | `Assistant is typing` | typing `aria-label` |
| `:1157` | `Working…` | slow-response hint |
| `:1186-1187` | `Sorry, I couldn't reach the library assistant just now. Please try again in a moment.` | network-error message |
| `:1193` | `Try again` | retry button text |
| `:1244` | `Try asking:` | starter-questions label |
| `:1344` | `Shrink chat` / `Expand chat` | expand `aria-label`, toggled |

Notes for the Spanish decision, from what this inventory shows:
- Nine of the nineteen are `aria-label` values. A translation pass that only covers visible text will leave a screen-reader user with a half-Spanish interface, which is worse than either extreme.
- `:1060` builds a plural by string concatenation (`"Sources (" + n + ")"`). Spanish pluralisation differs, so this needs a form-selecting helper, not a lookup table.
- The answers themselves are not in this list. They come from the model, so bilingual answers are a backend and system-prompt matter, and per D-20260729-2 the *rendering* of them stays in the widget. The widget's job in a bilingual world is to mark up the language it was handed, which needs [F8](#f8)'s contract change.
- Not user-visible, but for completeness: `widget.js:1298` logs `[gavilan-widget] query failed:` to the console.

The demo page carries roughly 40 more English strings. It is a demo artifact that the library never
embeds, so I have not inventoried it.

<a id="a9"></a>
### 9. Forms and errors

**Labelling.** The textarea has a solid accessible name via `aria-label` (`widget.js:1005`), so 4.1.2
passes. It is not placeholder-*only* in the accessibility sense, though the placeholder is the only
*visible* cue and it disappears on input ([F15](#f15)). There is no `<label>` element in the widget.

**Errors are conveyed in text, not by colour.** `appendError` writes a full sentence
(`widget.js:1185-1187`), so 1.4.1 Use of Color and 3.3.1 Error Identification are both satisfied.
The error bubble is appended to the live region, so the announcement path is the same one that works
for normal answers. Two qualifications: the bubble carries no `role="alert"`, so it is announced
politely rather than assertively (defensible for a chat surface, and arguably better); and the
intended red styling never actually renders ([F7](#f7)), which means the error state currently has
*no* visual differentiation at all - text only. Nothing fails, but the visual channel is weaker than
the code suggests.

**Retry** is a real keyboard-operable button, awkwardly placed relative to focus ([F16](#f16)).

**The character cap** stops input silently ([F12](#f12)).

<a id="a10"></a>
### 10. The demo page

Structurally the demo page is in better shape than the widget. Verified correct: `lang="en"`
(`demo-site.html:2`), a descriptive `<title>`, exactly one `<h1>`, sound landmarks (`main`, `nav
aria-label="Library sections"`, `header`, `footer`, `aside`), `role="note"` on the demo banner,
`<svg role="img" aria-label="Open book">` on the mark, real `<label for>` on all three sliders, and a
cost disclosure that is a real button with correct `aria-expanded` + `aria-controls` and resolvable
IDs (Enter activates it; exercised). All 40 text pairs pass 1.4.3, and its only 1.4.11 shortfalls are
decorative rules or the deliberately disabled search control, which 1.4.11 exempts.

It reflows cleanly - no horizontal scroll at 640px or 320px, and both grids collapse to one column.

**It does not interfere with the widget's focus behaviour.** Exercised on the demo page specifically:
nothing takes focus on load, there is no `autofocus`, the launcher opens the panel from the keyboard,
focus moves into the composer, Escape closes it, and focus returns to the launcher - identical to the
plain-host-page harness. The one interaction worth noting is quantitative rather than a defect: the
demo page has 20 focusable stops (23 with the cost panel open), so it is the page that makes
[F4](#f4)'s missing focus containment
concretely felt.

Findings: [D1](#d1) heading order (moderate), [D2](#d2) no skip link (minor, arguably N/A),
[D3](#d3) slider read-outs not announced (minor), [D4](#d4) ungated smooth scroll (AAA, advisory).

---

## Method and limits

**Tooling.** Chrome 150.0.7871.187 headless, driven over the DevTools Protocol from a dependency-free
Node 22 script, with `widget.js` copied unmodified into a scratch harness page carrying a stubbed
`fetch` and four focusable host-page controls placed *before* the widget (so focus leaking out of the
panel would be visible). Key presses were dispatched via `Input.dispatchKeyEvent`, so Tab, Enter,
Space and Escape went through the browser's real focus machinery rather than being simulated with
synthetic JS events. Colours were read with `getComputedStyle` and ratios computed with the WCAG
relative-luminance formula, compositing alpha in gamma-encoded sRGB the way browsers do. The demo
page was audited with its three deploy-time placeholders stamped into a scratch copy; the committed
file was never modified.

**What I could not check.**
- **No screen reader was run.** Not NVDA, JAWS, VoiceOver, TalkBack, or Orca. Every statement about announcement is therefore framed as a structural precondition, and [F1](#f1), [F2](#f2) and [F3](#f3) should be re-tested with a real screen reader before anyone reports them as resolved. This is the single biggest limit on this audit.
- **No real host page.** The widget was tested in a synthetic host page and on the demo page, not on `https://www.gavilan.edu/library/`. The host page's own background colour affects [F5](#f5), and its focusable-element count affects how bad [F4](#f4) feels.
- **Chrome only.** Firefox and Safari were not tested. Two findings are user-agent-dependent: the placeholder contrast ([item 6](#a6), 4.61:1 is Chrome's default and other engines ship lighter defaults) and the retry button's fallback focus ring ([F17](#f17)).
- **No deployed backend.** Answers came from a stub matching the documented `{answer, sources}` contract. Nothing was deployed and `cdk deploy` was not run.
- **Voice control was not exercised.** [F14](#f14) is derived from the accessible name versus the visible label, which is what 2.5.3 specifies; it was not confirmed against Dragon or Voice Control.

**Two results discarded as measurement artifacts,** rather than reported as findings:

1. A first pass appeared to show that **Enter did not activate the sources disclosure** - which would have been a Level A blocker. It was my harness: dispatching `keyDown`-with-text *and* a `char` event fired the click twice, toggling the disclosure open then shut so the net state looked unchanged. Re-tested with an instrumented click counter: Enter fires exactly one click, Space fires one, `aria-expanded` flips correctly. **The widget is fine.**
2. A first pass appeared to show the **panel overflowing the top of the viewport at 200% zoom** (`top: -136px`). That came from modelling zoom with CSS `zoom: 2`, under which `vh` keeps resolving against the full unzoomed viewport - so `calc(100vh - 64px)` did not shrink. Real browser zoom shrinks the CSS viewport instead. Re-measured properly at 640x512, 426x341 and 320x256: the panel fits at every level. **The widget is fine**, and its viewport clamping is doing exactly what it was written to do.

**Reproducing this.** The harness and probe scripts live in the session scratchpad, not in the repo -
they are throwaway. The contrast arithmetic is the WCAG formula against the colour values cited
inline, so every number above can be re-derived from `widget.js` and `demo-site.html` alone.

---

## Conformance summary

| | Level A | Level AA |
|---|---|---|
| **Failed** | 1.3.1, 2.4.3, 2.5.3 | 1.4.11, 4.1.3 |
| **Passed (verified)** | 1.4.1, 2.1.1, 2.1.2, 3.1.1, 3.3.1, 3.3.2, 4.1.2 | 1.4.3, 1.4.4, 1.4.10, 2.4.6 (widget), 2.4.7, 3.1.2 (English only) |
| **Not assessed** | criteria needing a screen reader, real AT, or media (none of the widget's content is time-based) | as left |

Five distinct success criteria are failed, none of them by a blocker, and the top five fixes above
clear four of the five (1.3.1, 2.4.3, 2.5.3, and 1.4.11 partially; 4.1.3 is fix #2). All five fixes are
inside `frontend/widget.js`, need no dependency, no build step, and no change to the model's system
prompt - consistent with D-20260729-2 (rendering problems are fixed in the widget) and D-20260727-10
(prefer standard semantics over bespoke scripting: every fix above is a native element, a standard
ARIA attribute, or a colour value).

**What to tell the client.** The widget was built with real buttons, a named dialog, a correctly
configured polite live region, reduced-motion support, and text contrast that passes everywhere - so
the foundation is sound and there is no accessibility blocker to deployment. Five criteria need work,
the largest being that screen-reader users cannot currently tell who said what in the conversation.
All of it is a day or two of work in a single file. The honest gap in this audit is that no screen
reader was run, and that should be scheduled before any conformance claim is made in writing.
