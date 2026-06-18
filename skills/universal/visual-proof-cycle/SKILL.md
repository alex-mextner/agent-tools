---
name: visual-proof-cycle
description: Use when you change anything user-visible — UI, a rendered image, a chart, generated output. Capture the result, read the capture back yourself, review it critically, fix what's wrong, and re-capture before claiming it works.
---

# Visual proof cycle

For anything with a visual output, "the code compiles" and "the test passes" do not
prove it *looks right*. The only proof is to look at the rendered result — and to
actually look, not just generate the image and move on.

## The cycle

1. **Capture** the real rendered output (a screenshot of the running app, the
   generated image, the chart). Drive the real render path, not a mock.
2. **Read it back yourself.** Open the capture and actually inspect it. An
   un-inspected screenshot is not proof — it's a file.
3. **Review critically.** Is the layout right? Is anything blank, clipped,
   overlapping, mis-colored, off by a theme? Compare against the intended design.
4. **Fix** what's wrong.
5. **Re-capture** and repeat until it's actually correct.

Only then claim it works, and attach the final capture as evidence.

## Common failure

Capturing a screenshot of an *idle* or *not-yet-rendered* state and calling it proof.
An empty panel, a loading spinner, or the wrong screen captured "successfully" proves
nothing. Make sure the capture shows the thing you changed, in its rendered state.

## When a before/after screenshot is impossible: attach a schematic

Sometimes there is nothing to screenshot — the change is docs, CI, or pure backend —
or you need to convey *how it should work*: a target state or design that doesn't
exist yet. In those cases the proof artifact is a **schematic** (SVG, mermaid, or
ASCII) instead of a capture. Attach it the same way you'd attach a screenshot.

The sanctioned generator is **codex** — ask it for a self-contained diagram:

```bash
# valid, self-contained SVG:
codex exec "Output ONLY a self-contained SVG diagramming X"
```

codex also emits mermaid flowcharts; SVG and mermaid can be attached directly.
If codex isn't available, hand-draw the schematic as ASCII or mermaid yourself —
the point is the artifact, not the generator.

Treat an LLM-generated SVG as untrusted: it's an active format that can carry
active content (e.g. `<script>`, event handlers, `href`/`xlink:href` fetches,
`<foreignObject>`). Default to rasterizing SVG → PNG before attaching,
using a renderer that executes no scripts and loads no external resources (`resvg`
is static by design — prefer it; librsvg's behavior is version-dependent). Attach
raw SVG only on a surface you've confirmed treats it as a static image. Mermaid and
ASCII carry no such risk — attach them directly.

## Why

Visual bugs are invisible to compilers and unit tests by definition. A human (or a
vision model — see `gan-critic-loop` and `review --visual`) has to look.
Building the look-review-fix loop into your definition of done is what stops "it
builds" from being mistaken for "it works".
