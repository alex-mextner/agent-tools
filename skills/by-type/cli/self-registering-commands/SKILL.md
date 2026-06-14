---
name: self-registering-commands
description: Use when building a multi-command CLI. Make commands self-registering — dropping a new command file into a directory adds the command, with zero edits to the dispatcher — instead of maintaining a central registry that every new command must touch.
---

# Self-registering command modules

A CLI with a central command registry (`commands = { build, test, deploy, ... }` that
every new command must be added to) creates a chokepoint: every command touches the
dispatcher, merge conflicts cluster there, and it's easy to forget to register a new one.
Make commands discover themselves instead.

## Pattern

The dispatcher **discovers** command modules from a directory at startup; each module
declares its own name and handler. Adding a command = dropping a file. No edit to the
dispatcher.

```
cli/
  dispatch.{ts,py}        # discovers and runs — never edited to add a command
  commands/
    build.{ts,py}         # exports { name: "build", run }
    test.{ts,py}          # exports { name: "test",  run }
    deploy.{ts,py}        # drop this file → `cli deploy` just works
```

```ts
// dispatch.ts — discovery, written once.
const modules = await loadCommandModules("./commands");   // glob the dir
const command = modules.find((m) => m.name === argv[0]);
await command.run(argv.slice(1));
```

Each command module owns its name, help text, and run function. The contract is the
module's exported shape, not a line in a central list.

## Why

Self-registration removes the dispatcher as a coupling point: contributors add a file and
nothing else, parallel work doesn't collide in a shared registry, and you can't ship a
command you forgot to wire up. It also keeps each command self-contained — name, help,
and logic in one file — which pairs with `cli/help-docs-sync` and `cli/lazy-heavy-imports`
(each module lazy-loads its own heavy deps).
