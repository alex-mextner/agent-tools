---
name: swift-concurrency
description: Use when writing Swift async/await, actors, tasks, or dealing with data-race safety and Sendable — structuring concurrent work, isolating shared mutable state, and avoiding priority inversion / detached-task leaks. Triggers on async/await, Task, actor, @MainActor, Sendable, or a Swift concurrency data-race warning. Applies to any Swift stack (lang-level), not just SwiftUI.
---

# Swift structured concurrency

`async/await` plus actors give you concurrency that the compiler can check for data races
(strict concurrency). Lean on structure — the task tree — instead of firing detached tasks
and hoping.

## Prefer structured tasks; detached is a last resort

- `async let` for a fixed set of concurrent children you then `await`.
- `withTaskGroup` for a dynamic number of homogeneous children.
- `.task { }` in SwiftUI ties work to a view's lifetime and cancels automatically.
- `Task.detached` breaks structure: it loses the parent's priority, task-locals, and
  cancellation. Use it only when you genuinely need to escape the current context, and then
  own its cancellation yourself.

```swift
// concurrent, structured, cancellation-aware:
async let profile = client.fetchProfile()
async let posts   = client.fetchPosts()
let screen = try await Screen(profile: profile, posts: posts)
```

## Isolate shared mutable state in an actor

An `actor` serializes access to its mutable state, so cross-task races become impossible by
construction. Reads/writes from outside are `await`ed. Do not reach for locks/queues by hand
when an actor expresses the same intent and is compiler-checked.

```swift
actor ImageCache {
    private var store: [URL: Image] = [:]
    func image(for url: URL) -> Image? { store[url] }
    func insert(_ image: Image, for url: URL) { store[url] = image }
}
```

## `@MainActor` for anything touching UI

UI state must be mutated on the main actor. Annotate view models / UI-facing types
`@MainActor` so the compiler enforces it, instead of sprinkling `DispatchQueue.main.async`.
Hop *off* the main actor for heavy work (`await Task.detached`-free: call an `async` function
that is not main-actor-isolated) and hop back by returning to a `@MainActor` context.

## Respect cancellation

Long-running work must check `Task.isCancelled` (or call `try Task.checkCancellation()`) at
loop boundaries and after each `await`, and clean up. A view that disappears cancels its
`.task`; a reducer effect that is superseded is cancelled — your code only honors that if it
actually checks.

## Sendable is the race-freedom contract

A type crossing a concurrency boundary must be `Sendable` (value types usually are; reference
types need to be immutable or internally synchronized, e.g. an actor). Don't silence a
Sendable warning with `@unchecked Sendable` unless you have *manually* guaranteed the
synchronization and left a comment saying how — an unchecked lie is a latent data race.

## Don't block an async context

Never call a blocking API (`Data(contentsOf:)`, `sleep`, a semaphore `wait`) from inside an
`async` function on a cooperative-pool thread — it starves other tasks. Use the async
equivalent (`URLSession.data`, `Task.sleep`, `await`).
