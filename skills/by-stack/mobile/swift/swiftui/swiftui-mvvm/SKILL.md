---
name: swiftui-mvvm
description: Use when building SwiftUI screens with an MVVM structure — deciding what belongs in the View vs an ObservableObject/@Observable view model, wiring state, and keeping views testable. Triggers on SwiftUI view models, @Published/@Observable state, and "where does this logic go" in a SwiftUI feature.
---

# SwiftUI MVVM: thin views, testable view models

In SwiftUI the `View` is already a declarative function of state — MVVM works *with* that,
not against it. The view model owns state and behavior; the view only renders it and
forwards user intent. Keep the view free of anything you'd want to unit-test.

## What goes where

- **View** — layout, styling, and `Button(action: viewModel.submit)` style intent
  forwarding. No networking, no formatting logic, no branching business rules.
- **View model** — an `@Observable` (iOS 17+) or `ObservableObject` type holding the
  screen's state and the methods that mutate it. It depends on protocols (a `Repository`,
  a `Clock`), never on concrete singletons, so it is testable without the UI.
- **Model** — plain value types (`struct`), Codable DTOs, domain entities. No UI, no
  framework imports beyond `Foundation`.

## Prefer `@Observable` over `ObservableObject` (iOS 17+)

```swift
@Observable
final class ProfileViewModel {
    private(set) var state: LoadState<Profile> = .idle
    private let repo: ProfileRepository

    init(repo: ProfileRepository) { self.repo = repo }

    func load() async {
        state = .loading
        do { state = .loaded(try await repo.fetch()) }
        catch { state = .failed(error) }
    }
}
```

`@Observable` tracks reads at the property level, so a view that only reads `state`
re-renders only when `state` changes — no `@Published` fan-out, no `objectWillChange`
noise. In the view, hold it with `@State` (owning) or pass it plainly (borrowing).

## Own the view model at the right level

- The screen that *creates* the feature owns it: `@State private var vm = ProfileViewModel(repo: …)`.
- A child view that only reads it takes it as a plain `let vm: ProfileViewModel` — do not
  re-wrap someone else's view model in a new `@State`, that forks the state.

## Model loading/error explicitly, never with scattered bools

A single `enum LoadState { case idle, loading, loaded(T), failed(Error) }` beats three
`isLoading`/`error`/`data` properties that can contradict each other. The view switches on
it; there is exactly one source of truth for "what is on screen right now".

## Keep side effects in `async` methods, not in `body`

`body` must be a pure function of state. Kick off work from `.task { await vm.load() }` or a
button action — never perform a fetch or mutate state as a side effect of rendering.

## Test the view model, snapshot the view

Because the view model has no UI dependency, test it directly: inject a fake repository,
call `await vm.load()`, assert on `vm.state`. Reserve snapshot/UI tests for layout, not for
logic that already lives in a testable view model.
