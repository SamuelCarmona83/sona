---
version: alpha
name: spoty-scanner-explorer-design
description: |
  Terminal-native data explorer rendered entirely in monospaced type — every label,
  stat, table cell, and footer line uses the same face. The page reads like a
  manpage or static-site README: warm cream canvas (#fdfcfc), nearly-black ink
  (#201d1d), 4px-radius rectangles for interactive elements, and bracketed
  [+]/[-]/[x] ASCII markers as bullets. The only dark surface is a hero card
  that mocks the bot's Discord TUI: black background, pipe characters, and an
  ASCII wordmark. Sections are hairline-bordered text blocks on cream with no
  shadows, gradients, or decorative imagery.

colors:
  primary: "#201d1d"
  on-primary: "#fdfcfc"
  ink: "#201d1d"
  ink-deep: "#0f0000"
  charcoal: "#302c2c"
  body: "#424245"
  mute: "#646262"
  stone: "#6e6e73"
  ash: "#9a9898"
  canvas: "#fdfcfc"
  surface-soft: "#f8f7f7"
  surface-card: "#f1eeee"
  surface-dark: "#201d1d"
  surface-dark-elevated: "#302c2c"
  hairline: "rgba(15,0,0,0.12)"
  hairline-strong: "#646262"
  on-dark: "#fdfcfc"
  on-dark-mute: "#9a9898"
  accent: "#007aff"
  accent-hover: "#0056b3"
  warning: "#ff9f0a"
  danger: "#ff3b30"
  success: "#30d158"

typography:
  display-xl:
    fontFamily: "Berkeley Mono, JetBrains Mono, IBM Plex Mono, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace"
    fontSize: 28px
    fontWeight: 700
    lineHeight: 1.5
  heading-md:
    fontFamily: "{typography.display-xl.fontFamily}"
    fontSize: 16px
    fontWeight: 700
    lineHeight: 1.5
  body-md:
    fontFamily: "{typography.display-xl.fontFamily}"
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.5
  body-strong:
    fontFamily: "{typography.display-xl.fontFamily}"
    fontSize: 14px
    fontWeight: 500
    lineHeight: 1.5
  caption-md:
    fontFamily: "{typography.display-xl.fontFamily}"
    fontSize: 12px
    fontWeight: 400
    lineHeight: 2
  button-md:
    fontFamily: "{typography.display-xl.fontFamily}"
    fontSize: 14px
    fontWeight: 500
    lineHeight: 2

rounded:
  sm: 4px
  none: 0px

spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  section: 48px

components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.button-md}"
    rounded: "{rounded.sm}"
    padding: 4px 16px
    height: 36px
  button-secondary:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
    border: "1px solid {colors.hairline}"
    rounded: "{rounded.sm}"
  button-tab-active:
    textColor: "{colors.ink}"
    borderBottom: "1px solid {colors.ink}"
  button-tab:
    textColor: "{colors.mute}"
  text-input:
    backgroundColor: "{colors.surface-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: 8px 12px
    height: 40px
  hero-tui-mockup:
    backgroundColor: "{colors.surface-dark}"
    textColor: "{colors.on-dark}"
    padding: 32px 24px
  stat-tile:
    backgroundColor: "{colors.canvas}"
    border: "1px solid {colors.hairline}"
    padding: 16px
  section-block:
    border: "1px solid {colors.hairline}"
    padding: 24px
  badge-news:
    backgroundColor: "{colors.surface-dark}"
    textColor: "{colors.on-dark}"
    rounded: "{rounded.sm}"
    padding: 2px 8px
  install-snippet:
    backgroundColor: "{colors.surface-card}"
    rounded: "{rounded.sm}"
    padding: 12px 16px
---

## Overview

Spoty Scanner's data explorer follows a **manpage-first** visual system: one monospaced family across every role, cream canvas, hairline section borders, and ASCII bracket glyphs instead of icon fonts. The dark hero block is reserved for the page header — a faux terminal showing bot status and cache path.

**Key characteristics**

- [+] 100% monospaced typography — Berkeley Mono preferred, JetBrains Mono / IBM Plex Mono as OSS substitutes
- [+] `{colors.canvas}` (#fdfcfc) body background — no alternating section fills
- [+] `{colors.surface-dark}` (#201d1d) hero only — terminal mockup, not global chrome
- [+] `{rounded.sm}` (4px) on buttons, inputs, badges; sections use `{rounded.none}`
- [+] `[+]` / `[-]` / `[x]` ASCII markers for lists, warnings, and feature bullets
- [+] `{spacing.section}` (48px) vertical rhythm between major blocks
- [+] Semantic colors (`{colors.accent}`, `{colors.warning}`, `{colors.danger}`, `{colors.success}`) for links, dedupe warnings, errors, and disk-ok states

## Colors

### Chrome
| Token | Hex | Use |
|---|---|---|
| `{colors.canvas}` | #fdfcfc | Page body, cards, inputs on focus |
| `{colors.surface-soft}` | #f8f7f7 | Input default background |
| `{colors.surface-card}` | #f1eeee | Code snippets, disabled buttons |
| `{colors.hairline}` | rgba(15,0,0,0.12) | Section and table borders |

### Text
| Token | Hex | Use |
|---|---|---|
| `{colors.ink}` | #201d1d | Headlines, primary labels |
| `{colors.body}` | #424245 | Table cells, card body |
| `{colors.mute}` | #646262 | Metadata, inactive tabs |
| `{colors.ash}` | #9a9898 | Disabled, placeholders |

### Semantic
| Token | Hex | Use |
|---|---|---|
| `{colors.accent}` | #007aff | Links, active sort indicator |
| `{colors.warning}` | #ff9f0a | Duplicate-group banner |
| `{colors.danger}` | #ff3b30 | Dedupe CTA, error banner |
| `{colors.success}` | #30d158 | On-disk yes, success banner |

## Typography

Single stack for all roles:

```
Berkeley Mono, JetBrains Mono, IBM Plex Mono, ui-monospace,
SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, Courier New
```

| Role | Size | Weight | Use |
|---|---|---|---|
| display-xl | 28px (22px mobile) | 700 | Page title in hero |
| heading-md | 16px | 700 | Section labels, tab strip |
| body-md | 14px | 400 | Tables, cards, inputs |
| caption-md | 12px | 400 | Footer, stat labels |
| button-md | 14px | 500 | All buttons |

## Layout

- **Content column:** max-width 1120px, centered, 24px horizontal padding (16px mobile)
- **Stats row:** 2-up mobile → 3-up tablet → 5-up desktop; each `{component.stat-tile}`
- **Tab strip:** hairline bottom rule; active tab `{component.button-tab-active}`
- **Tables:** full-width inside `{component.section-block}`, horizontal scroll on narrow viewports
- **Cards grid:** 1 → 2 → 3 → 4 columns by breakpoint; sharp corners, hairline border

## Components

### Hero (`{component.hero-tui-mockup}`)
Dark block at page top. Contains ASCII wordmark, one-line status (`cache: /spotify_cache/`), and a prompt row (`> !play …`) in `{colors.surface-dark-elevated}`.

### Stat tile (`{component.stat-tile}`)
Large numeric value in `{colors.ink}` 700; caption in `{colors.mute}` uppercase via `{typography.caption-md}`.

### Dedupe banner
`{colors.warning}` text on `{colors.canvas}` with 1px `{colors.warning}` border at 30% opacity. CTA uses danger secondary styling.

### Data table
Header row `{colors.mute}` caption weight; row hover `{colors.surface-soft}`; sort arrow `{colors.accent}`.

## Responsive

| Breakpoint | Behavior |
|---|---|
| ≥1024px | 5 stat tiles, 4-column card grid |
| ≥768px | Horizontal tabs, 3-column grid |
| <768px | 2 stat tiles per row, 1-column grid, stacked toolbar |

## Iteration guide

1. Reference tokens as `{colors.ink}`, not hex literals in prose.
2. Do not introduce sans-serif or display faces.
3. Keep `{colors.surface-dark}` to the hero only.
4. New UI states → new component entries (`-active`, `-disabled`), not inline one-offs.
5. Prefer ASCII `[+]` bullets over SVG icons.

## Implementation

Styles are implemented with **Tailwind CSS CDN** in [`web/explorer.html`](explorer.html):

- `tailwind.config.theme.extend` maps design tokens (`ink`, `canvas`, `mute`, `warn`, etc.) to utility classes
- Reusable class strings live in the `UI` object in the page script (buttons, tabs, tables, cards)
- No custom `<style>` block; spinner uses Tailwind `animate-spin`
- JetBrains Mono loaded via Google Fonts; applied with `font-mono`

When adding UI, prefer Tailwind utilities and extend the theme before introducing inline CSS.