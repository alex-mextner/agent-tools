/**
 * index.test.ts — the extension WIRING (the security-critical map from a decision onto pi's
 * block/confirm API). policy.test.ts covers the pure matcher; this covers the handler:
 * deny → block, allow → run, ask → confirm, and the fail-closed no-UI branch.
 * Run via `npm test` (tsx --test).
 */

import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

// Point the policy loader at an empty dir BEFORE guard() runs so it falls back to the baked
// baseline (git push --force => deny, pkill => ask) — deterministic regardless of this machine's
// real ~/.pi/agent policy. guard() calls loadPolicy() lazily in its body, so setting this here
// (module scope, before any harness() call) is enough.
process.env.PI_CODING_AGENT_DIR = mkdtempSync(join(tmpdir(), "pg-index-"));

const { default: guard } = await import("./index.ts");

type Handler = (event: unknown, ctx: unknown) => Promise<unknown>;

function rawHandler(): Handler {
	let handler: Handler | undefined;
	const pi = { on: (ev: string, h: Handler) => { if (ev === "tool_call") handler = h; } };
	guard(pi as never);
	if (!handler) throw new Error("extension did not register a tool_call handler");
	return handler;
}

function harness() {
	const handler = rawHandler();
	return (command: string, ctx: unknown) => handler({ toolName: "bash", input: { command } }, ctx);
}

const uiYes = { hasUI: true, ui: { select: async () => "Yes" } };
const uiNo = { hasUI: true, ui: { select: async () => "No" } };
const noUI = { hasUI: false, ui: { select: async () => { throw new Error("must not prompt without UI"); } } };

test("deny → { block: true } with the rule reason", async () => {
	const r = (await harness()("git push --force", uiYes)) as { block: boolean; reason: string };
	assert.equal(r.block, true);
	assert.match(r.reason, /force-push/i);
});

test("allow → undefined (command runs)", async () => {
	assert.equal(await harness()("git status", uiYes), undefined);
});

test("non-bash tool call is ignored (undefined)", async () => {
	const handler = rawHandler();
	const r = await handler({ toolName: "read", input: { path: "x" } }, uiYes);
	assert.equal(r, undefined);
});

test("ask + UI 'Yes' → allowed (undefined)", async () => {
	assert.equal(await harness()("pkill node", uiYes), undefined);
});

test("ask + UI not-'Yes' → blocked", async () => {
	const r = (await harness()("pkill node", uiNo)) as { block: boolean };
	assert.equal(r.block, true);
});

test("ask + NO UI → blocked (fail-closed, non-interactive)", async () => {
	const r = (await harness()("pkill node", noUI)) as { block: boolean; reason: string };
	assert.equal(r.block, true);
	assert.match(r.reason, /no UI/i);
});
