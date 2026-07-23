/**
 * permission-guard/loader.ts — resolve + read the rig-written policy file, fail-closed.
 *
 * The policy lives at `${PI_CODING_AGENT_DIR:-~/.pi/agent}/rig-permission-policy.json` (rig owns
 * it — see rig-cli riglib/actions/runner.py `_do_provision_pi_extension`). This module resolves
 * that path and loads it, ALWAYS returning a usable Policy:
 *   - file missing            → DEFAULT_POLICY (clean machine / policy not yet provisioned)
 *   - file unreadable/invalid → DEFAULT_POLICY + a stderr warning (FAIL-CLOSED: the baked denies
 *                               still fire, so a corrupt config can never silently open the gate)
 * Kept separate from policy.ts (pure, fs-free) so the matcher stays unit-testable without fs/env.
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { coercePolicy, DEFAULT_POLICY, type Policy } from "./policy.ts";

export const POLICY_FILENAME = "rig-permission-policy.json";

/** The pi agent config dir (PI_CODING_AGENT_DIR override, else ~/.pi/agent). */
export function piAgentDir(env: NodeJS.ProcessEnv = process.env): string {
	return env.PI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");
}

export function policyPath(env: NodeJS.ProcessEnv = process.env): string {
	return join(piAgentDir(env), POLICY_FILENAME);
}

export interface LoadResult {
	policy: Policy;
	/** Non-null when we fell back to DEFAULT_POLICY because the on-disk file was broken. */
	warning: string | null;
}

export function loadPolicy(env: NodeJS.ProcessEnv = process.env): LoadResult {
	const path = policyPath(env);
	let text: string;
	try {
		text = readFileSync(path, "utf8");
	} catch (err) {
		if ((err as NodeJS.ErrnoException)?.code === "ENOENT") {
			// genuinely missing — clean machine or not yet provisioned. Silent baked baseline.
			return { policy: DEFAULT_POLICY, warning: null };
		}
		// present but unreadable (permission/ownership drift, a directory in its place, …): this is
		// NOT the clean-machine case — a rig-installed stricter policy could be silently disabled,
		// so surface the fallback instead of failing silently.
		return {
			policy: DEFAULT_POLICY,
			warning: `permission-guard: policy at ${path} is unreadable (${String(err)}); using baked baseline (fail-closed)`,
		};
	}
	let parsed: unknown;
	try {
		parsed = JSON.parse(text);
	} catch (err) {
		return {
			policy: DEFAULT_POLICY,
			warning: `permission-guard: policy at ${path} is not valid JSON (${String(err)}); using baked baseline (fail-closed)`,
		};
	}
	const policy = coercePolicy(parsed);
	if (!policy) {
		return {
			policy: DEFAULT_POLICY,
			warning: `permission-guard: policy at ${path} has an invalid shape; using baked baseline (fail-closed)`,
		};
	}
	return { policy, warning: null };
}
