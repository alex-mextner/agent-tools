import { defineConfig } from "oxlint";

// Canonical output for Rig's built-in TypeScript baseline. Rig renders the effective
// repository config from built-in defaults < global config < rig.yaml; projects should
// change policy in Rig configuration rather than hand-editing generated Oxc files.
export default defineConfig({
  options: {
    typeAware: true,
  },
  jsPlugins: [
    {
      name: "anti-slop",
      specifier: "./tools/oxlint/anti-slop/index.ts",
    },
  ],
  categories: {
    correctness: "error",
    suspicious: "error",
    perf: "error",
  },
  rules: {
    "anti-slop/no-chained-type-assertions": "error",
    "anti-slop/no-known-value-widening": "error",
    "anti-slop/no-widen-then-assert": "error",
    "anti-slop/no-unsafe-dictionary-type": "error",
    "anti-slop/require-safety-comment-for-type-assertion": "error",
    "anti-slop/no-object-parameters": "error",
    "anti-slop/no-unknown-type-aliases": "error",
    "anti-slop/no-unknown-returns": "error",

    "anti-slop/no-reflect-get": "warn",
    "anti-slop/no-reflect-apply": "warn",
    "anti-slop/no-module-mocking": "warn",
    "anti-slop/no-unknown-parameters": "warn",

    "anti-slop/no-runtime-typeof": "off",
    "anti-slop/no-conditional-empty-object-spread": "off",
    "anti-slop/no-shape-in-symbol-names": "off",

    "typescript/no-unsafe-type-assertion": "error",
    "typescript/no-unnecessary-type-assertion": "error",
    "typescript/no-non-null-assertion": "error",
    "typescript/ban-ts-comment": [
      "error",
      {
        "ts-ignore": true,
        "ts-nocheck": true,
        "ts-expect-error": "allow-with-description",
        minimumDescriptionLength: 8,
      },
    ],
  },
});
