# Design
<!-- impeccable:design-schema 1 -->

Landing page for sensortap at sensortap.baselashraf.com. One surface: Persuade.

Seed key `2f7c9b41`. Direction: instrument readout — a lab bench display, not a
marketing page pretending to be one.

## The world

sensortap's whole claim is that it can show you something true about your own
machine that you've never seen. The page performs that claim rather than
describing it: the hero is a live-look terminal panel showing the framework's
actual, real output from a real machine (a Dell G3 3779), not a mockup and not
placeholder digits.

This refuses the tool-landing-page default of a soft gradient hero with a
vague product shot. There is no product shot — there is a terminal, because
that's the actual interface most of this audience will meet first.

Distinct from the two sibling subdomains sharing this credit footer:
- winduo.baselashraf.com is a painted Prussian-blue flat, an object with a
  hinge. sensortap is not an object, it's a readout.
- roadwright.baselashraf.com is warm editorial paper-and-ink, a mathematical
  essay. sensortap is not an essay, it's a console.

## Colour

**Instrument dark.** Near-black ground (`#0a0d0f`), the resting state of a
diagnostics screen with the lights off. One accent, a phosphor amber
(`#ffb454`) — not green, deliberately avoiding the "hacker terminal" cliché —
reserved for live/measured values and the primary action. A second, cooler
signal blue (`#5fb3d9`) marks structural chrome (prompts, borders, section
numerals) so amber stays exclusively the colour of "this is a real reading."

| Token | Value | Role |
|---|---|---|
| `--bg` | `#0a0d0f` | Page ground |
| `--bg-panel` | `#101418` | Terminal panel, cards |
| `--bg-panel-raised` | `#161b20` | Nested rows (sensor list items) |
| `--line` | `#242b31` | Hairlines, borders |
| `--ink` | `#e8ecee` | Primary text, 14.1:1 on bg |
| `--ink-dim` | `#9aa5ab` | Secondary text, 6.7:1 |
| `--ink-faint` | `#5f6a70` | Captions, comments, 4.1:1 |
| `--amber` | `#ffb454` | Live values, primary CTA |
| `--amber-dim` | `#8a6a3c` | Amber at rest / borders |
| `--blue` | `#5fb3d9` | Prompts, structural marks, links |
| `--green` | `#7ec98f` | "present" status only |
| `--red-dim` | `#b06a5c` | "absent" status only, desaturated — not an error |

Every text/background pair meets WCAG AA on its own ground.

## Type

- **Display & body: Inter** — a plain, competent grotesk. This page has no
  display-face ambitions; the terminal panel carries all the visual voice.
- **Everything measured, coded, or commanded: JetBrains Mono.** Sensor ids,
  kinds, commands, the whole terminal panel, install snippets. This is the
  one typographic decision that matters: if it's a fact about the machine or
  a thing you'd type, it's mono; if it's the author's sentence, it's Inter.

## Composition

- One `.shell` measure (74rem), matching the parent portfolio's own token so
  the nav feels continuous when a visitor arrives from baselashraf.com.
- The hero terminal panel is real HTML text, not a screenshot: selectable,
  reflows, and works with CSS off (though its layout depends on CSS being
  present to look like a terminal rather than a wall of text).
- No cards-of-icon-plus-heading. Sensor kinds are rendered as a dense,
  scannable list — the density itself is the argument ("look how much this
  finds").
- One authored moment: the terminal panel's prompt cursor blinks
  (`prefers-reduced-motion` turns it into a static block). Nothing else
  animates on load; scroll-triggered reveals are refused as a category
  default here — a diagnostics screen doesn't perform for you, it just
  reports.

## Icons

None invented. The only marks are the terminal's own glyphs (`$`, `❯`) and
the nav's existing theme/hamburger icons carried over from the sibling
sites' shared vocabulary — this page has no separate icon system to keep
consistent.

## Accessibility

- Skip link, single `h1`, ordered headings.
- The terminal panel's live output is in a `<pre>`/`<code>` region with
  `aria-label` summarizing it in prose, so a screen reader gets the sentence
  version, not 86 lines read literally.
- `:focus-visible` is a 2px amber outline.
- `prefers-reduced-motion: reduce` removes the cursor blink and any
  scroll-linked effect.
- No horizontal scroll at 360px.

## Copy

Plain and specific, matching this whole conversation's standing rule against
invented claims. Every number on this page (86 sensors, 16 kinds, 9 Windows
adapters) is real, produced by running the actual CLI, and is re-verified
before publish rather than typed from memory.
