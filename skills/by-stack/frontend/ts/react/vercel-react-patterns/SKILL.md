---
name: vercel-react-patterns
description: Use when writing or reviewing React / Next.js components for performance and correct data flow — server vs client components, data fetching, memoization, and bundle size. Triggers on React components, Next.js App Router pages, "use client", useEffect data fetching, or a component re-rendering too often.
---

# React / Next.js performance patterns

Fast React is mostly about *where* code runs and *how much* re-renders. Decide the boundary
first, keep client bundles small, and let the framework fetch on the server.

## Server components by default; `"use client"` only where needed

In the Next.js App Router a component is a Server Component unless it opts out. Keep the
`"use client"` boundary as *low* in the tree as possible — a leaf that needs `onClick` or
`useState`, not the whole page. Everything above it renders on the server, ships zero JS, and
can `await` data directly.

```tsx
// server component — fetches on the server, no client JS
export default async function Page() {
  const posts = await getPosts()          // no useEffect, no loading spinner
  return <PostList posts={posts} />        // PostList can be server too
}
```

## Do not fetch in `useEffect` when the server can fetch

`useEffect(() => { fetch(...) }, [])` creates a client waterfall: render → mount → fetch →
re-render, plus a loading state you now have to manage. Fetch in a Server Component (or a
route handler / server action) instead. Reserve client fetching for genuinely
client-only, interaction-driven data.

## Measure before you memoize

`useMemo`/`useCallback`/`React.memo` are not free — they add allocation and comparison cost
and clutter. Add them when a profiler shows a real, expensive re-render, not preemptively.
The common real wins:

- Memoize an expensive *computation*, not a cheap object literal.
- Wrap a child in `React.memo` only when its props are stable and it re-renders needlessly.
- Give `useCallback` to a memoized child's handler prop (otherwise a new function identity
  defeats the `memo`).

## Keep state as local and as low as possible

Lifting state higher than necessary re-renders a bigger subtree on every change. Co-locate
state with the component that uses it; lift only when two siblings truly share it. Derive,
don't duplicate — compute from existing state during render rather than syncing a second
state with `useEffect`.

## Stable keys, never the array index

Use a stable identity (`item.id`) as the `key`. An index key makes React mis-associate rows
on insert/reorder, corrupting local state and hurting reconciliation.

## Watch the client bundle

Every import in a client component ships to the browser. Import heavy libraries in Server
Components, `next/dynamic` for code-splitting client-only widgets, and prefer
tree-shakeable named imports. Check that a "small" component didn't pull a large dependency
across the `"use client"` line.

## Suspense + streaming for perceived speed

Wrap slow server data in `<Suspense fallback={…}>` so the shell streams immediately and the
slow part fills in — better perceived performance than blocking the whole route on the
slowest query.
