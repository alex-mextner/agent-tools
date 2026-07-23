/**
 * loader.test.ts — unit tests for the fail-closed policy loader.
 * Run: `npm test` (in this dir) → `tsx --test policy.test.ts index.test.ts loader.test.ts`.
 */

import assert from "node:assert/strict";
import { chmodSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { loadPolicy, POLICY_FILENAME, policyPath } from "./loader.ts";
import { DEFAULT_POLICY } from "./policy.ts";

function tmpAgentDir(): string {
	return mkdtempSync(join(tmpdir(), "pg-loader-"));
}

test("policyPath joins PI_CODING_AGENT_DIR with the fixed filename", () => {
	const dir = tmpAgentDir();
	assert.equal(policyPath({ PI_CODING_AGENT_DIR: dir }), join(dir, POLICY_FILENAME));
});

test("missing policy file: baked baseline, no warning (clean machine)", () => {
	const dir = tmpAgentDir();
	const { policy, warning } = loadPolicy({ PI_CODING_AGENT_DIR: dir });
	assert.deepEqual(policy, DEFAULT_POLICY);
	assert.equal(warning, null);
});

test("invalid JSON: baked baseline + a warning", () => {
	const dir = tmpAgentDir();
	writeFileSync(join(dir, POLICY_FILENAME), "{ not json", "utf8");
	const { policy, warning } = loadPolicy({ PI_CODING_AGENT_DIR: dir });
	assert.deepEqual(policy, DEFAULT_POLICY);
	assert.match(warning ?? "", /not valid JSON/);
});

test("malformed policy shape: baked baseline + a warning", () => {
	const dir = tmpAgentDir();
	writeFileSync(join(dir, POLICY_FILENAME), JSON.stringify({ version: 1 }), "utf8");
	const { policy, warning } = loadPolicy({ PI_CODING_AGENT_DIR: dir });
	assert.deepEqual(policy, DEFAULT_POLICY);
	assert.match(warning ?? "", /invalid shape/);
});

test("valid policy file: loaded verbatim, no warning", () => {
	const dir = tmpAgentDir();
	const doc = { version: 1, default: "deny", rules: [] };
	writeFileSync(join(dir, POLICY_FILENAME), JSON.stringify(doc), "utf8");
	const { policy, warning } = loadPolicy({ PI_CODING_AGENT_DIR: dir });
	assert.deepEqual(policy, doc);
	assert.equal(warning, null);
});

const CHMOD_UNREADABLE_SKIP =
	(process.getuid?.() === 0 && "root bypasses file permission bits, so chmod 0o000 would not reproduce EACCES") ||
	(process.platform === "win32" &&
		"Windows only honors the read-only bit, not chmod 0o000, so this would not reproduce EACCES either");

test("unreadable-but-present policy (e.g. permission/ownership drift): baked baseline + a warning, NOT the silent missing-file path", { skip: CHMOD_UNREADABLE_SKIP }, () => {
	const dir = tmpAgentDir();
	const path = join(dir, POLICY_FILENAME);
	writeFileSync(path, JSON.stringify({ version: 1, default: "deny", rules: [] }), "utf8");
	chmodSync(path, 0o000);
	try {
		const { policy, warning } = loadPolicy({ PI_CODING_AGENT_DIR: dir });
		assert.deepEqual(policy, DEFAULT_POLICY);
		assert.match(warning ?? "", /unreadable/);
	} finally {
		chmodSync(path, 0o644); // restore so the tmp dir can be cleaned up
	}
});

test("a directory in place of the policy file is also treated as unreadable, not missing", () => {
	const dir = tmpAgentDir();
	mkdirSync(join(dir, POLICY_FILENAME));
	const { policy, warning } = loadPolicy({ PI_CODING_AGENT_DIR: dir });
	assert.deepEqual(policy, DEFAULT_POLICY);
	assert.match(warning ?? "", /unreadable/);
});
