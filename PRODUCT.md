# Product
<!-- impeccable:product-schema 1 -->
<!-- Written by inference from the full request history, no interview available.
     Assumptions are labeled ASSUMED inline; everything else is stated fact from
     the codebase or the user's own words. -->

## Platform
web

<!-- The library/CLI itself is a desktop Python framework (Windows-first, Linux
     planned). This PRODUCT.md covers the WEB SURFACE ONLY: the landing page
     at sensortap.baselashraf.com. -->

## Stack
Delegated. Chosen and recorded here so later work does not reopen it.

**Landing page:** static HTML/CSS, no framework, no build step, no JS framework —
matches the pattern already established for winduo.baselashraf.com and
roadwright.baselashraf.com (the user's other subdomain projects). Deployed as a
static site (Vercel, matching the sibling projects — ASSUMED, ports the existing
pattern since no new instruction was given).

## Users
Primary: people who like tinkering with sensors — hobbyists, makers, and
developers who enjoy linking real hardware sensors to UI/UX or generative/cool
animations. The user explicitly named this audience and gave macOS's "Mac Duo"
and their own "WinDuo" project as the kind of thing this audience already likes:
small, sensor-driven, physically-grounded software toys.

Secondary: Python developers who want to read arbitrary sensors (camera, mic,
battery, motion, hardware monitors) through one API without hand-rolling
platform-specific code for each one.

Situation: someone finds the page from a link (baselashraf.com's work list, or a
share), is unfamiliar with sensortap, and decides in under a minute whether this
is worth `pip install`-ing. They may never open a terminal on this visit at all.

## Product Purpose
sensortap is a cross-platform Python framework that discovers every sensor a
computer has and exposes raw readings through one unified API — one registry,
many backend adapters, no adapter needing to know about any other. It is
discovery/plumbing infrastructure, not a collection of finished sensor apps: it
answers "what sensors does this machine have and how do I read them," so the
tinkerer builds the fun part (an animation, a visualization, a lid-close effect
like WinDuo) on top.

Verified today on the user's own G3 3779 laptop: 86 real sensors across 16
kinds — accelerometer, gyroscope, magnetometer, light, orientation, hinge angle,
battery (percentage + capacity), Wi-Fi signal, Bluetooth presence, touchpad
capability, camera, microphone, and — once the Windows Helper_Process (a bundled
.NET/LibreHardwareMonitor bridge) is running — 65 more: per-core CPU load,
temperatures, voltages, and clock speeds.

## Positioning
`pip install sensortap && sensortap list` prints every sensor a machine has,
including several most owners have never seen surfaced anywhere: a hinge-angle
sensor, per-core CPU load, Bluetooth radio presence as a plain flag. That single
command is the whole pitch — the page has to make a visitor believe that
sentence is literally true (it is) and want to run it.

The reference point the user gave directly: Mac Duo and WinDuo (the user's own
prior project, at winduo.baselashraf.com) are "sensor into cool UI/UX or
animation" toys built on exactly one sensor apiece, hand-wired per platform.
sensortap is the layer underneath that kind of project — it is what makes the
NEXT ten WinDuo-shaped ideas easy to build instead of a fresh reverse-engineering
project each time.

## Operating Context
- Windows first (9 real adapters shipped: WinRT motion/orientation/light/camera/
  audio, battery, radio, touchpad, plus the .NET Helper_Process bridge for
  hardware-monitoring sensors). Linux adapters are designed but not yet built
  (explicit contribution opportunity, same pattern as WinDuo's own README).
- The .NET helper is an optional install extra (`sensortap[hwmon]`) — the base
  `pip install sensortap` stays small and works with zero extra downloads;
  hardware-monitor sensors (temp/fan/voltage/clock/load) need the extra plus a
  bundled helper executable.
- Privacy-sensitive sensors (camera, microphone, touchpad capacitive image)
  are consent-gated: enumerating them never opens a device or triggers an OS
  permission prompt; reading one requires the caller to explicitly grant
  consent for that exact sensor id first.

## Capabilities and Constraints
Confirmed:
- One registry, many independent adapters — camera, microphone, motion,
  orientation, light, battery, radio, touchpad, and (Windows) a bundled
  .NET helper for CPU/GPU/board sensors via LibreHardwareMonitor.
- A CLI (`sensortap list` / `read` / `stream` / `inspect`) as the primary,
  fastest way to see the framework work — no code required for the demo.
- Also usable as a Python library: `sensortap.list_sensors()`, `.read()`,
  `.stream()`, `.consent()`.
- Real, unified schema across every sensor: kind, dtype, units, sampling
  rate, availability — verified end-to-end today on real hardware.
- Contributor-friendly: one adapter = one sensor family = one file,
  registered via a standard Python entry point, with a Conformance_Check any
  contributor can run against their own adapter with no source access needed.
- Free and open source (ASSUMED MIT-style permissive, matching the framework's
  own pyproject.toml license field — this is infrastructure aimed at
  maximizing adoption and contribution, unlike WinDuo which the user
  deliberately made noncommercial; no signal was given to make sensortap
  noncommercial too, and the tinkerer audience benefits from permissive reuse).
- No PyPI package published yet (ASSUMED not yet released, since no
  publish step has occurred in this session) — the landing page's primary
  call to action is therefore the GitHub repo and the `pip install` command
  stated as the near-term/actual command, not a "coming soon."

Constraints:
- This is a young, single-maintainer project. No adoption numbers, no
  testimonials, no press — none may be invented, consistent with the design
  ethic the user has held to on every other project in this conversation
  (WinDuo, Roadwright, Eigendrum).
- The Windows helper install is heavier (a bundled ~68 MB self-contained .NET
  executable) — this must not be hidden, but also must not overshadow the
  base install's lightness.

## Brand Commitments
Name: sensortap. Domain: sensortap.baselashraf.com (subdomain of the user's
personal site, matching winduo.baselashraf.com and roadwright.baselashraf.com).
Credit to Basel Ashraf, linked, matching every sibling project's nav pattern.
No donation/support links were requested for this project (unlike WinDuo) —
ASSUMED omitted unless later added, since this is infrastructure for other
makers rather than a personal effect app people "own" the way WinDuo's dimming
effect is owned.

## Evidence on Hand
- Real, live-verified sensor list from the user's own G3 3779 (86 sensors,
  16 kinds) — this is the single most persuasive fact available and the
  landing page's job is to surface it plainly, not the invented kind of
  "trust us" copy the rest of this conversation's design work has always
  refused to write.
- The GitHub repo at github.com/BaselAshraf81/sensortap (ASSUMED — matches
  the user's existing GitHub org and every sibling project's repo-naming
  convention; the repo has not yet been created in this session and must be
  created as part of this task).
- WinDuo (winduo.baselashraf.com, github.com/BaselAshraf81/winduo) as the
  direct, named prior-art link the user asked to include.

## Product Principles
1. **Prove it, don't sell it.** The one command and the one real sensor list
   from real hardware carry the whole page. No invented adoption claims.
2. **Infrastructure, not an app.** The page must not read like a finished
   consumer tool — it is the layer a tinkerer builds their own WinDuo-shaped
   idea on top of, and should make that framing explicit.
3. **The install stays honest about weight.** Base install is light; the
   optional hardware-monitor extra is heavier and openly labeled as such.
4. **Same family, different voice.** Visually kin to winduo.baselashraf.com
   and roadwright.baselashraf.com (subdomain pattern, credit footer, nav-back-
   to-baselashraf.com), but its own world — sensortap is a tool for other
   builders, not a personal effect, and should not borrow WinDuo's Prussian-
   blue "painted flat" metaphor wholesale.

## Accessibility & Inclusion
Standard web accessibility: semantic headings, sufficient contrast, keyboard-
operable nav, works with JavaScript disabled for all core content (matching
the sibling sites' committed practice of no-JS-required core content).
