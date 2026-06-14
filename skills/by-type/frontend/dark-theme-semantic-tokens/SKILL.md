---
name: dark-theme-semantic-tokens
description: Use when styling any UI component. Use semantic design tokens (bg-background, text-foreground, border-border) rather than hardcoded color values, so theming and dark mode work without per-component edits.
---

# Semantic color tokens, never hardcoded colors

A hardcoded color (`#fff`, `text-white`, `bg-gray-900`) is a color that's correct in
exactly one theme. The moment you support dark mode — or any second theme — every
hardcoded color is a bug you have to hunt down individually. Semantic tokens fix this
once, centrally.

## Rule

Style against **semantic tokens** that describe the *role*, and let the theme map roles
to actual colors:

```tsx
// BAD — locked to one theme; breaks in dark mode.
<div className="bg-white text-black border-gray-200">

// GOOD — role-based; the active theme decides the actual color.
<div className="bg-background text-foreground border-border">
```

The tokens (`background`, `foreground`, `muted`, `primary`, `border`, `card`, …) are
defined once per theme; components reference the role, never the literal. Switching to
dark mode (or any theme) then re-maps the roles centrally, and every component follows
with no per-component change.

## Why

The cost of hardcoded colors is paid at theming time, multiplied by every component — a
tedious, error-prone sweep where you inevitably miss a few and ship a white flash in dark
mode. Semantic tokens move the color decision out of the component and into the theme, so
"support dark mode" is a theme definition, not a thousand edits. This also keeps the
palette consistent — components can't invent off-palette colors when they only reference
roles. (The specific token names depend on your design system; the principle —
role-based, not literal — is universal.)
