---
name: version-changelog-assert
description: Use when a library or CLI tracks its version in a constant and keeps a CHANGELOG. Add a test that asserts the version constant has a matching CHANGELOG section, so you can't ship a release without documenting it.
---

# Assert the VERSION constant has a CHANGELOG entry

A version bump without a CHANGELOG entry ships a release nobody can tell apart from the
last one. The two live in different files and are easy to update independently — you bump
`VERSION` and forget the CHANGELOG, or write the CHANGELOG and forget to bump. Make them
fail the build when they disagree.

## Pattern

A small test that reads the version constant and asserts a matching section exists in the
CHANGELOG:

```ts
test("VERSION has a matching CHANGELOG section", () => {
  const version = VERSION;                       // the single source of the version
  const changelog = readFileSync("CHANGELOG.md", "utf8");
  expect(changelog).toContain(`## ${version}`);  // a section header for this version
});
```

Now you cannot bump the version without adding its changelog section (the test goes red),
and you can't add a changelog section for a version you forgot to set. Run it in the
pre-commit / CI gate so the coupling is enforced, not remembered. (See `git-hooks/` for
packaging it.)

## Why

The version and its changelog are two representations of the same fact — "this is release
X and here's what changed" — stored apart, so they drift. A test that binds them turns
"remember to update both" into a structural guarantee: the release is documented or it
doesn't build. Cheap to write, catches a sloppy-release class of mistake every time.
Pairs with `cli/help-docs-sync` — same "two artifacts that must agree, assert it" idea.
