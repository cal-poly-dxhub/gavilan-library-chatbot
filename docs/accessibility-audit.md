# Widget accessibility audit - WCAG 2.1 AA / Section 508

First accessibility assessment of the embeddable chat widget (`frontend/widget.js`) and the
shareable demo page (`frontend/demo-site.html`), against WCAG 2.1 Level AA - what Section 508
incorporates by reference (36 CFR 1194.1, E205.4). Level A and AA are in scope; AAA items were
recorded as advisory only.

**Audited 2026-07-29 against commit `6c07630`, then remediated in the same branch.** 22 findings:
18 in the widget, 4 in the demo page. No blockers - 4 serious, 7 moderate, 11 minor. All text
contrast already passed; the gaps were screen-reader semantics and focus containment. 14 of the
18 widget findings were fixed and re-measured. The four deferred ones and the demo-page findings
were re-checked against the current file on 2026-08-17 and all still stand.

**The one thing to know before quoting any of this: no screen reader was run.** Not NVDA, JAWS,
VoiceOver, TalkBack or Orca. Every claim here is a DOM fact or an accessibility-tree fact
measured in Chrome, which is where these defects live - but what a screen reader actually *says*
is unverified, and a session with one is the thing standing between this and a written
conformance claim.

Line references in the git history of this file point into `6c07630`. `widget.js` was 1,429 lines
then and has more than doubled since, so they no longer resolve; search for the symbol instead.

---

## Where it landed

| | Level A | Level AA |
|---|---|---|
| **Failed as audited** | 1.3.1, 2.4.3, 2.5.3 | 1.4.11, 4.1.3 |
| **After remediation** | none in the widget; 1.3.1 + 2.4.6 still fail on the demo page (D1) | none in the widget |
| **Passed, verified** | 1.4.1, 2.1.1, 2.1.2, 3.1.1, 3.3.1, 3.3.2, 4.1.2 | 1.4.3, 1.4.4, 1.4.10, 2.4.6 (widget), 2.4.7, 3.1.2 (English only) |
| **Not assessed** | anything depending on what a screen reader says | as left |

Contrast, computed from the values the browser actually resolves (including alpha compositing):
84 pairs. All 58 text pairs pass 1.4.3 - 18 in the widget, 40 on the demo page - including the
composer placeholder, which is left to the user-agent default and measures 4.61:1 in Chrome. The
1.4.11 failures were all non-text: focus indicators and control boundaries.

Reflow and zoom pass. The panel clamps both dimensions to the viewport
(`width: min(384px, calc(100vw - 32px))`, `height: min(560px, calc(100vh - 64px))`), measured
fully inside the viewport with no horizontal document scroll at 200%, 300% and 400% zoom and at a
320px viewport. Wide tables scroll inside their own container, which 1.4.10 explicitly permits.
The honest caveat is usability rather than conformance: at the 400% test size the conversation
area is 79px tall, about one suggestion chip. It conforms; it is also cramped, and worth knowing
before a client tries it.

## What was fixed

| SC | Finding | Re-measured result |
|---|---|---|
| 1.3.1 | Turns carried no speaker attribution - a screen reader could not tell a question from an answer | An `.sr-only` label is the first child of every turn. Chrome's tree carries non-ignored `"You said:"` / `"Library assistant said:"` across a 5-turn conversation, where all seven nodes previously returned `role: null, ariaLabel: null` |
| 4.1.3 | The "thinking" live region had no text - `role="status"` with three `aria-hidden` dots and the message in `aria-label` | `role="status"` moved to the bubble, message moved into the content (19 chars, was 0). The slow-response note is now created and appended into that same region when the timer fires, so it is an insertion rather than an un-hiding outside it |
| 2.4.3 | Focus escaped the open panel, and Escape only worked from inside it | `aria-modal="true"` + Tab wrapping. 0 of 14 Tab stops and 0 of 8 Shift+Tab stops leave the widget, where 5 consecutive stops used to land outside. Escape now closes from a host-page control and returns focus to the launcher |
| 2.4.3 | Focus landed mid-dialog, past the greeting and starter questions | The composer carries `aria-describedby` to the greeting bubble on first launch, dropped with the chips on the first message so later turns are not prefixed by a stale description |
| 1.4.11 | Focus rings measured 1.77:1 against the page behind them | Two-tone ring - see below |
| 1.4.11 | Typing dots, composer border and chip border all under 3:1 | One token, `--line: #7d8894`: dots 3.25:1 (was 2.27), composer border 3.61:1 (was 1.57), chip border 3.48:1 (was 1.31) |
| 2.5.3 | The launcher's accessible name did not contain its visible label, so voice control could not activate it by name | The `aria-label` is gone; the accessible name is the button's own text, `"Ask the Library"`. `aria-haspopup="dialog"` carries what the label used to hint at |
| 1.3.1 | Markdown headings rendered as bold paragraphs | Real `h3`-`h6` (`#` -> h3, offset by two because the host page owns h1/h2, clamped at h6), with `font-size: 1em` pinned so a real h5 does not render smaller than body text |
| 1.3.1 | Table header cells had no `scope` | `scope="col"` on header cells, none on data cells |
| - | The error bubble's red styling never applied - `.bubble--error` (0,1,0) lost to `.msg--bot .bubble` (0,2,0), so a failed send looked byte-identical to an answer | Selector deepened to `.msg--bot .bubble.bubble--error`. Renders `#fdecea` / `#8a1c12` at 8.14:1 |
| 2.4.3 | After a failed send, "Try again" sat behind focus | A failed send focuses the retry button, verified through a fully keyboard-driven send |
| - | `.retry` was the one control with no widget-defined focus style | `.retry:focus-visible` with the same two-tone treatment, 14.79:1 against the error bubble |
| 3.1.2 | The widget declared no language of its own | The chrome declares its language rather than inheriting the host page's. Superseded since by the bilingual work, which stamps `lang` on every message - but with the *selected* UI language, not one the backend reports, so a Spanish answer in an English UI is still marked `en`. Closing that needs a language field on the `/query` response |

### The focus ring, and why it is two colours

No single flat colour clears 3:1 against all four surfaces the ring can land on. So it is a dark
outline with a light halo filling the `outline-offset` gap: whichever surface it lands on, one of
the two halves carries the contrast, and the two contrast with **each other** at 16.91:1 - which
is what makes it robust on a host page whose background the widget cannot know, including a dark
one.

| Background | Dark outline `#1a1d21` | Light halo `#ffffff` | Ring vs background |
|---|---|---|---|
| White host page `#ffffff` | **16.91:1** | 1:1 | **16.91:1** |
| Demo page `#f6f4f2` | **15.42:1** | 1.1:1 | **15.42:1** |
| Widget thread `#fafbfc` | **16.32:1** | 1.04:1 | **16.32:1** |
| Maroon launcher/Send fill `#8a1c30` | 1.84:1 | **9.17:1** | **9.17:1** |

Read the maroon row the way 1.4.11 intends: the fill is the colour adjacent on the inside, so the
halo does the work there; on the outside it is always the dark outline. Confirmed in rendered
pixels, not just CSS values - a scan across the focused launcher's left edge reads
`#f4f4f4 | #1f2226 #1a1d21 #1a1d21 | #f8f9f9 #ffffff | #90293c #8a1c30`, i.e. page, 3px dark
outline, 2px white halo, maroon fill.

Applied to the five controls whose rings were failing or unstyled: launcher, Send, composer,
suggestion chips, retry. Rings that already passed are untouched. The composer keeps its
brand-tinted `border-color` on focus (3.09:1), which is what still makes a focused field read as
this widget; the ring itself is ink, because a tint of `#8a1c30` cannot reach 3:1 against a light
page at any opacity that still looks like a tint.

## Deliberately not fixed

- **Four decorative boundaries** - table cell borders and the sources rule at 1.16:1, the panel border at 1.35:1. 1.4.11 does not apply to purely decorative boundaries, the panel has a drop shadow doing the separating work, and darkening table gridlines to 3:1 makes an hours table read as a spreadsheet.
- **The typing dots at partial opacity.** They pass at 3.25:1 at full opacity, but the blink animation dips them to 30%, where no colour can reach 3:1 (the arithmetic ceiling is about 2.2:1). The keyframes were left alone: reduced-motion users see the static full-opacity colour, and the pending state is now carried as text as well, which is the more reliable channel.
- **`aria-controls` on the sources disclosure.** Nothing fails - 4.1.2 is satisfied by `aria-expanded` alone, and `aria-controls` is an APG recommendation with inconsistent screen-reader support. It needs a unique id per message plus plumbing on both ends.
- **A character counter at the 1000-char composer cap.** No criterion clearly fails. A counter is a new visible element and a copy decision (when it appears, whether it goes assertive), which is a product call rather than a conformance fix.
- **A persistent visible label on the composer.** The accessible name is solid so 4.1.2 passes, and for a chat box next to a Send button the purpose stays obvious. This is a stated position, not an oversight - "placeholder as the only visible label" is the most common finding in external audits and it is better to have an answer ready.
- **`px` -> `rem`/`em`.** 1.4.4 passes as measured, so this is preference-honouring, not a fix. It also does not stop at CSS: `autosize()` hardcodes the same 120px ceiling as `max-height`, so converting the CSS alone would desynchronise the textarea's growth from its clamp.
- **The demo page (D1-D4).** This pass was widget-only. D1 - two `<h2>`s before the only `<h1>`, because the cost panel sits above the masthead in DOM order - is the one with a real Level A/AA mapping and the one to do next. D2 (no skip link) is arguably N/A on a single page, D3 (slider read-outs not announced) is minor, D4 (ungated smooth scroll) is Level AAA.

## Already correct - please do not "fix" these

This list exists so a later pass does not regress work that is already right.

**Keyboard and focus.** The launcher is a real `<button type="button">`; Enter and Space both
open the panel. Every interactive control is a native `<button>`, `<a>` or `<textarea>` - there
is no div-with-click-handler anywhere, so nothing is mouse-only. Opening moves focus into the
panel, closing returns it to the launcher, and nothing steals focus on load. Tab order follows
DOM order; there is no positive `tabindex` in the file.

**The live region** - the part most chat widgets get wrong, and it is already right. The thread
is `role="log"` + `aria-live="polite"` + `aria-relevant="additions"` with `aria-atomic` left
unset. That means an arriving answer is announced as a single addition and the thread is **not**
re-read on every update. `aria-atomic` must stay unset for that to remain true.

**Semantics.** The panel is `role="dialog"` with an accessible name. Every icon-only button has a
name and every inline SVG is `aria-hidden` + `focusable="false"`, so no icon leaks into the tree.
`aria-expanded` and `aria-pressed` track state in both directions. Markdown tables render as real
`<table>`/`<thead>`/`<th>`. Answers are built with `createElement`/`textContent` only, never
`innerHTML`, pinned by contract test.

**No Shadow DOM ARIA trap.** Worth checking, came back clean: there are zero ID-based ARIA
references in the widget, so nothing fails silently across the shadow boundary. The constraint to
carry forward, since several fixes suggest adding such references: an ID reference works fine
when **both ends are inside the same shadow root**. The trap fires only if something inside
points at an id in the host page, or vice versa - and that failure is silent, with no console
error.

**Motion.** Both widget animations are gated on `prefers-reduced-motion: reduce` and both
collapse under emulation.

**Demo page structure.** `lang="en"`, one `<h1>`, sound landmarks, `role="note"` on the demo
banner, real `<label for>` on all three sliders, and a cost disclosure with correct
`aria-expanded` + `aria-controls` whose ids resolve. It reflows to 320px and does not interfere
with the widget's focus behaviour.

## Method and limits

Chrome 150 headless, driven over the DevTools Protocol from a dependency-free Node script, with
`widget.js` copied unmodified into a scratch harness page carrying a stubbed `fetch` and four
focusable host-page controls placed *before* the widget, so focus leaking out of the panel would
be visible. Key presses went through `Input.dispatchKeyEvent`, so Tab, Enter, Space and Escape
used the browser's real focus machinery rather than synthetic JS events. Colours were read with
`getComputedStyle` and ratios computed with the WCAG relative-luminance formula, compositing
alpha in gamma-encoded sRGB the way browsers do. The demo page was audited with its three
deploy-time placeholders stamped into a scratch copy; the committed file was never modified.

What this could not check, beyond the screen reader: no real host page (the widget was tested in
a synthetic harness and on the demo page, not on gavilan.edu, and the host's background affects
the focus ring while its focusable-element count affects how bad the containment gap feels);
Chrome only, so the placeholder contrast and the retry button's fallback ring are user-agent
specific; no deployed backend, with answers from a stub matching the `{answer, sources}`
contract; and voice control was not exercised, so the label-in-name finding is derived from the
accessible name versus the visible label, which is what 2.5.3 specifies.

**Two results were discarded as measurement artifacts** rather than reported, because a wrong
finding is worse than a missing one. A first pass appeared to show Enter not activating the
sources disclosure, which would have been a Level A blocker - it was the harness firing the click
twice, toggling the disclosure open then shut. And a first pass appeared to show the panel
overflowing the viewport at 200% zoom, which came from modelling zoom with CSS `zoom: 2`, under
which `vh` keeps resolving against the full unzoomed viewport. Real browser zoom shrinks the CSS
viewport instead. Both times, the widget was fine.

The harness and probe scripts were throwaway and are not in the repo. The contrast arithmetic is
the WCAG formula against the colour values cited inline, so every number here can be re-derived
from `widget.js` and `demo-site.html` alone.

## What to tell the client

The widget was built with real buttons, a named dialog, a correctly configured polite live
region, reduced-motion support and text contrast that passes everywhere, so there was no
accessibility blocker to deployment. Five criteria needed work and all five have been fixed and
re-measured in a browser: the conversation now says who spoke, the panel keeps keyboard focus and
closes on Escape from anywhere, the focus indicator clears 3:1 on every background it can land
on, the pending state carries text instead of only animated dots, and the launcher can be
activated by the words printed on it. The demo page has one remaining heading-order issue, which
is demo scaffolding rather than anything the library embeds.

One caveat is unchanged and should be said plainly: no screen reader has been run.
