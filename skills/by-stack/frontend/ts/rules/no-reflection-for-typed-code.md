# No reflection for ordinary typed code

## Avoid

`Reflect.get`, `Reflect.apply`, runtime shape probing, and module mocking when the dependency or property is statically known.

## Prefer

Use direct typed property access/calls, explicit interfaces, and dependency injection through real seams owned by the application.

## Why

Reflection and module mocking bypass the contracts TypeScript is able to check. They are appropriate only when the runtime problem is genuinely dynamic; anti-slop adds focused rules for the common cases where reflection is being used as an escape hatch rather than as a real requirement.
