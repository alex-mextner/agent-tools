---
name: wiring-only-top-component
description: Use when a top-level / container component is accumulating state, effects, and callbacks. Keep the top component as thin wiring and move state/effects/handlers into dedicated hooks, so the container stays readable and the logic stays testable.
---

# Keep the top component wiring-only

A container/editor/page component naturally attracts everything — a dozen `useState`s, a
pile of `useEffect`s, inline event handlers, data fetching. Left unchecked it becomes a
500-line component nobody can follow and nothing can test in isolation. Keep it thin.

## Pattern

The top component does **wiring**: it composes children and passes props. The *logic* —
state, effects, derived values, callbacks — lives in dedicated hooks.

```tsx
// Logic extracted into focused, testable hooks.
function Editor() {
  const selection = useSelection();
  const document  = useDocument();
  const { onKeyDown, onDrop } = useEditorInteractions(document, selection);

  // The component body is just composition — easy to read at a glance.
  return (
    <Frame onKeyDown={onKeyDown} onDrop={onDrop}>
      <Toolbar selection={selection} />
      <Canvas document={document} selection={selection} />
    </Frame>
  );
}
```

Each hook (`useSelection`, `useDocument`, `useEditorInteractions`) owns one concern, can
be unit-tested without rendering the whole tree, and can be reused.

## Why

A wiring-only top component is readable — you see the shape of the UI in a few lines
without wading through effect dependencies and state juggling. The extracted hooks are
individually testable (`renderHook`) and individually replaceable. The alternative — all
logic inline in the container — couples unrelated concerns, makes every change risky, and
makes testing require rendering the entire subtree. This is the React-shaped application of
`universal/smallest-change` and separation of concerns.
