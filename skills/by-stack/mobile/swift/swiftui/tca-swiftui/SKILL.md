---
name: tca-swiftui
description: Use when building a SwiftUI feature with The Composable Architecture (TCA) — modeling State/Action/Reducer, isolating side effects in dependencies, and composing features. Triggers on TCA Reducer, Store, @Reducer, Effect, or "how do I structure this feature" in a point-free TCA codebase.
---

# The Composable Architecture in SwiftUI

TCA makes a feature a value: `State` (what is), `Action` (what happened), and a `Reducer`
(how state evolves and what effects run). Reach for it when you want exhaustive testability
and composition; it is heavier than plain MVVM, so use it deliberately, not by default.

## The four parts

- **State** — a `struct` holding everything the feature needs to render. Equatable.
- **Action** — an `enum` of every event: user intent (`.submitTapped`) AND results
  (`.profileResponse(Result<Profile, Error>)`). Name actions for what *happened*, not what
  to *do*.
- **Reducer** — the `@Reducer` type whose `body` maps `(inout State, Action) -> Effect`.
  Pure: it only mutates state and returns effects. No `URLSession`, no `Date()`, no
  randomness inline.
- **Store** — the runtime object a SwiftUI view observes (`@Bindable var store`).

```swift
@Reducer
struct Profile {
    @ObservableState
    struct State: Equatable { var profile: Profile?; var isLoading = false }

    enum Action { case onAppear, response(Result<Profile, Error>) }

    @Dependency(\.profileClient) var profileClient

    var body: some ReducerOf<Self> {
        Reduce { state, action in
            switch action {
            case .onAppear:
                state.isLoading = true
                return .run { send in
                    await send(.response(Result { try await profileClient.fetch() }))
                }
            case let .response(result):
                state.isLoading = false
                state.profile = try? result.get()
                return .none
            }
        }
    }
}
```

## Side effects live in dependencies, never in the reducer body

Anything non-deterministic — network, disk, `Date`, `UUID`, a clock — is a
`@Dependency`. The reducer stays a pure function, so a test controls every input by
overriding the dependency. This is the whole point: `TestStore` asserts that each action
produces exactly the expected state mutation and follow-up effects, and it FAILS if any
effect is left unaccounted for.

## Compose, don't nest conditionals

Build big features from small ones with `Scope`, `forEach`, and `ifLet`. A child feature is
a full `@Reducer`; the parent embeds its `State`/`Action` and scopes the store. Prefer many
small reducers over one reducer with deep switch arms.

## Keep the view dumb

The SwiftUI view reads `store.state` and sends `store.send(.submitTapped)`. It contains no
logic — all of it is in the reducer, where it is exhaustively testable. If you feel like
putting an `if` with business meaning in the view, it belongs in the reducer.

## When NOT to use TCA

A leaf screen with trivial local state does not need a Store, dependencies, and a TestStore.
Use `swiftui-mvvm` (or plain `@State`) there and save TCA for features whose logic and
effects genuinely benefit from exhaustive testing and composition.
