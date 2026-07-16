/**
 * permission-guard/policy.ts — the pure, dependency-free permission matcher.
 *
 * What this is
 * ------------
 * The pi coding agent ships NO built-in permission system: it runs bash with the user's own
 * privileges (github.com/earendil-works/pi docs — "Pi does not include a built-in permission
 * system"). This module is the deterministic policy engine the `permission-guard` pi extension
 * (index.ts) uses to decide, for one bash command string, whether it is `allow` / `ask` / `deny`.
 * It is factored out of the extension wiring so it can be unit-tested with zero pi/runtime deps.
 *
 * How it is reached at runtime
 * ----------------------------
 * `index.ts` registers a `pi.on("tool_call")` handler; for `event.toolName === "bash"` it calls
 * `evaluateCommand(event.input.command, policy)` and maps the decision onto pi's block/confirm API.
 * The `policy` object is loaded from the rig-written JSON at `${PI_CODING_AGENT_DIR:-~/.pi/agent}/
 * rig-permission-policy.json` (rig owns the policy; the extension is generic). When that file is
 * absent or unparseable the extension falls back to `DEFAULT_POLICY` below — fail-closed: the
 * dangerous set stays denied even with no/broken config.
 *
 * Matching model (mirrors the argv-level intent of the rig agent-hooks, NOT prefix globs)
 * --------------------------------------------------------------------------------------
 * A rule matches a command when the command's argv0 basename equals `rule.command`, every token in
 * `rule.argvAll` appears somewhere in the argv, and — if `rule.flagsAny` is set — at least one of
 * those flag tokens appears anywhere as an exact token. Flag-ANYWHERE exact-token matching is why
 * this catches `git commit -m "..." --no-verify` (which the claude-code prefix patterns cannot) and
 * why `--force` never false-matches `--force-with-lease` (a different, non-equal token). Compound
 * commands (`a && b`, `a | b`, `a; b`) are split and each sub-command evaluated independently, and
 * the STRONGEST decision across all sub-commands and all rules wins (deny > ask > allow) — a
 * denied clause anywhere in a chain denies the whole command.
 *
 * SYNC: the DEFAULT_POLICY rule set is the cross-language twin of rig's PI_DENY_RULES / PI_ASK_RULES
 * in rig-cli riglib/permissions.py. rig writes the SAME rules into the policy JSON; keep both in
 * step (they encode the one rig baseline: gh pr merge, force-push, --no-verify, sudo rm,
 * screencapture => deny; pkill/killall/git reset --hard => ask).
 */

export type Decision = "allow" | "ask" | "deny";

export interface PolicyRule {
	/** Stable identifier, used in reasons/diagnostics. */
	id: string;
	action: "ask" | "deny";
	/** argv0 basename the rule applies to (e.g. "git", "gh", "sudo", "screencapture"). */
	command: string;
	/** All of these tokens must appear in argv[1:] (subcommand words like ["pr","merge"]). */
	argvAll?: string[];
	/** At least one of these exact tokens must appear anywhere in argv (flags like ["--force","-f"]). */
	flagsAny?: string[];
	/** Human-readable explanation surfaced to the agent/user when the rule fires. */
	reason: string;
}

export interface Policy {
	version: number;
	/** Decision when no rule matches. Normally "allow". */
	default: Decision;
	rules: PolicyRule[];
}

export interface Evaluation {
	decision: Decision;
	reason?: string;
	ruleId?: string;
}

/**
 * The baked baseline — used verbatim when the rig-written policy file is missing or unparseable.
 * Fail-closed: the dangerous denies still fire with zero/broken config. Keep in SYNC with rig's
 * riglib/permissions.py PI_DENY_RULES / PI_ASK_RULES.
 */
export const DEFAULT_POLICY: Policy = {
	version: 1,
	default: "allow",
	rules: [
		{
			id: "gh-pr-merge",
			action: "deny",
			command: "gh",
			argvAll: ["pr", "merge"],
			reason: "raw `gh pr merge` is banned — PR merges go through the gated `gh ship` delegator",
		},
		{
			id: "git-force-push",
			action: "deny",
			command: "git",
			argvAll: ["push"],
			flagsAny: ["--force", "-f"],
			reason: "force-push rewrites shared history; use --force-with-lease if you truly must",
		},
		{
			id: "git-no-verify",
			action: "deny",
			command: "git",
			flagsAny: ["--no-verify"],
			reason: "--no-verify bypasses the commit/push guard hooks; run the hooks",
		},
		{
			id: "sudo-rm",
			action: "deny",
			command: "sudo",
			argvAll: ["rm"],
			reason: "no legitimate agent flow removes files as root",
		},
		{
			id: "screencapture",
			action: "deny",
			command: "screencapture",
			reason: "screenshots go through Playwright/CDP; screencapture black-frames off-Space windows",
		},
		{
			id: "pkill",
			action: "ask",
			command: "pkill",
			reason: "broad pattern-kills have nuked other sessions' work — confirm the target",
		},
		{
			id: "killall",
			action: "ask",
			command: "killall",
			reason: "broad pattern-kills have nuked other sessions' work — confirm the target",
		},
		{
			id: "git-reset-hard",
			action: "ask",
			command: "git",
			argvAll: ["reset"],
			flagsAny: ["--hard"],
			reason: "git reset --hard destroys uncommitted work — confirm first",
		},
	],
};

const _SEVERITY: Record<Decision, number> = { allow: 0, ask: 1, deny: 2 };

/**
 * Split a command line into top-level sub-commands on `&&`, `||`, `|`, `;` and newlines.
 * Operators inside single/double quotes are ignored so a quoted literal (e.g. a commit message
 * that contains `;`) does not spuriously split. This is intentionally a lightweight splitter, not a
 * full shell parser — it mirrors what the rig agent-hooks do (evaluate each clause independently).
 */
export function splitSubCommands(command: string): string[] {
	const parts: string[] = [];
	let cur = "";
	let quote: '"' | "'" | null = null;
	for (let i = 0; i < command.length; i++) {
		const ch = command[i];
		const next = command[i + 1];
		if (quote) {
			cur += ch;
			if (ch === quote) quote = null;
			continue;
		}
		if (ch === '"' || ch === "'") {
			quote = ch;
			cur += ch;
			continue;
		}
		// a bare `&` that is part of a redirection (`2>&1`, `&>file`) is NOT a clause separator —
		// splitting there would strand a trailing guarded flag in a clause with the wrong argv0.
		const isRedirectAmp = ch === "&" && (cur.trimEnd().endsWith(">") || next === ">");
		if ((ch === "\n" || ch === ";" || ch === "|" || ch === "&") && !isRedirectAmp) {
			// consume a paired operator (&&, ||) as one separator
			if ((ch === "&" && next === "&") || (ch === "|" && next === "|")) i++;
			parts.push(cur);
			cur = "";
			continue;
		}
		cur += ch;
	}
	parts.push(cur);
	return parts.map((p) => p.trim()).filter((p) => p.length > 0);
}

/**
 * Tokenize one sub-command by whitespace, honoring single/double quotes and stripping the
 * surrounding quote characters from each token. Backslash-escapes are left as-is (over-matching a
 * guard rule is safe; under-matching is not).
 */
export function tokenize(sub: string): string[] {
	const tokens: string[] = [];
	let cur = "";
	let quote: '"' | "'" | null = null;
	let started = false;
	for (let i = 0; i < sub.length; i++) {
		const ch = sub[i];
		if (quote) {
			if (ch === quote) quote = null;
			else cur += ch;
			continue;
		}
		if (ch === '"' || ch === "'") {
			quote = ch;
			started = true;
			continue;
		}
		// Unquoted redirection metachars (`>`, `<`, `&`) are token boundaries, not part of a command
		// name or flag: bash tokenizes `--force&>out` / `--force>out` into `--force` + the redirect,
		// so a guarded flag glued to a redirect must NOT weld into one non-matching token (that would
		// be a deny bypass). Quoted occurrences are preserved by the quote branch above.
		// Treat CR as whitespace too: a trailing `\r` (CRLF input) glued to a token defeats
		// exact-token flag matching (`--force\r` !== `--force`).
		if (ch === " " || ch === "\t" || ch === "\r" || ch === ">" || ch === "<" || ch === "&") {
			if (started) {
				tokens.push(cur);
				cur = "";
				started = false;
			}
			continue;
		}
		cur += ch;
		started = true;
	}
	if (started) tokens.push(cur);
	return tokens;
}

function basename(token: string): string {
	const slash = token.lastIndexOf("/");
	return slash === -1 ? token : token.slice(slash + 1);
}

/**
 * Skip leading environment-assignment prefixes so the real argv0 is resolved. Handles both the
 * bare inline form (`FOO=1 git push --force`) and an explicit `env` runner (`env FOO=1 git …`,
 * `FOO=1 env BAR=2 git …`) — otherwise argv0 would be `FOO=1` / `env` and a guarded command would
 * slip past. (Full wrapper/substitution evasion — `sh -c '…'`, `$(…)` — is out of scope for a
 * lightweight matcher, exactly as it is for the argv-level agent-hooks and the prefix rules.)
 */
const _ASSIGN_RE = /^[A-Za-z_][A-Za-z0-9_]*=/;
// GNU env long options that take a REQUIRED separate-token argument which is NOT the command (so
// the arg must be skipped past). Only --unset/--chdir qualify: their arg is mandatory, so the
// separate-token form is valid. Deliberately EXCLUDED:
//   - --split-string: its argument IS the command (must stay visible as argv0);
//   - --block-signal/--default-signal/--ignore-signal: OPTIONAL args, so getopt_long only accepts
//     the attached `--opt=SIG` form (handled by the `--opt=value` branch) — a separate token is
//     NEVER the value, so skipping it would swallow the real command → a deny bypass.
const _ENV_LONG_ARG_OPTS = new Set(["--unset", "--chdir"]);

/**
 * getopt semantics for a GNU `env` short-flag cluster (the chars after the leading `-`): does the
 * option consume the NEXT token as its argument? env's arg-taking short options are `-u` (unset)
 * and `-C` (chdir); `-S` (split-string) is special — its value is the COMMAND, never a skippable
 * arg. Scanning left→right for the first such letter:
 *   -u / -iu      → arg letter is LAST in the cluster → argument is a SEPARATE token → true
 *   -uC / -uNAME  → arg letter is NOT last → argument is ATTACHED (rest of this token) → false
 *   -S / -iS      → split-string → arg is the command → false (leave the next token visible)
 *   -i / -0 / -v  → no arg-taking letter → false
 */
function _shortClusterTakesSeparateArg(cluster: string): boolean {
	for (let k = 0; k < cluster.length; k++) {
		const c = cluster[k];
		if (c === "S") return false; // split-string: its argument is the command
		if (c === "u" || c === "C") return k === cluster.length - 1; // separate iff nothing attached
	}
	return false;
}

function stripEnvPrefix(tokens: string[]): string[] {
	let i = 0;
	while (tokens[i] && _ASSIGN_RE.test(tokens[i])) i++;
	if (tokens[i] && basename(tokens[i]) === "env") {
		i++;
		// Consume env's own options + assignments so argv0 resolves to the real binary, not a flag:
		// `env -i git …`, `env -C dir git …`, `env -u NAME git …`, `env --unset NAME git …`,
		// `env -iu NAME git …`, `env -- git …`. This is best-effort argv0 recovery for the COMMON
		// `env` forms an agent emits — NOT a hardened sandbox. Exotic obfuscation (`env -S "git …"`
		// as one quoted string, `sh -c '…'`, `$(…)`) stays out of scope, exactly as it is for the
		// argv-level agent-hooks and the claude-code prefix rules (the deep layer underneath).
		while (tokens[i]) {
			const t = tokens[i];
			if (_ASSIGN_RE.test(t)) {
				i++;
				continue;
			}
			if (t === "--") {
				i++;
				break;
			}
			if (t.startsWith("--")) {
				i++;
				// `--opt=value` is self-contained; `--opt value` skips the separate value for the
				// arg-taking long opts. --split-string's value IS the command → never skip it.
				if (_ENV_LONG_ARG_OPTS.has(t) && tokens[i] !== undefined) i++;
				continue;
			}
			if (t.length > 1 && t.startsWith("-")) {
				i++;
				// Short-flag cluster: apply getopt semantics to decide whether the NEXT token is
				// this option's argument (and so not the command). See _shortClusterTakesSeparateArg.
				if (_shortClusterTakesSeparateArg(t.slice(1)) && tokens[i] !== undefined) i++;
				continue;
			}
			break;
		}
	}
	return tokens.slice(i);
}

function ruleMatchesTokens(rule: PolicyRule, tokens: string[]): boolean {
	if (tokens.length === 0) return false;
	if (basename(tokens[0]) !== rule.command) return false;
	const rest = tokens.slice(1);
	if (rule.argvAll) {
		for (const need of rule.argvAll) {
			if (!rest.includes(need)) return false;
		}
	}
	if (rule.flagsAny && rule.flagsAny.length > 0) {
		if (!rule.flagsAny.some((f) => tokens.includes(f))) return false;
	}
	return true;
}

/**
 * Evaluate one bash command string against a policy. Returns the strongest decision across every
 * sub-command and every rule (deny beats ask beats the policy default). The returned reason/ruleId
 * belong to the first rule that produced the winning decision.
 */
export function evaluateCommand(command: string, policy: Policy): Evaluation {
	let best: Evaluation = { decision: policy.default };
	for (const sub of splitSubCommands(command)) {
		const tokens = stripEnvPrefix(tokenize(sub));
		for (const rule of policy.rules) {
			if (!ruleMatchesTokens(rule, tokens)) continue;
			if (_SEVERITY[rule.action] > _SEVERITY[best.decision]) {
				best = { decision: rule.action, reason: rule.reason, ruleId: rule.id };
			}
		}
	}
	return best;
}

/** Narrow an unknown parsed JSON value to a Policy, or return null (caller fails closed). */
export function coercePolicy(value: unknown): Policy | null {
	if (typeof value !== "object" || value === null) return null;
	const v = value as Record<string, unknown>;
	if (!Array.isArray(v.rules)) return null;
	const rules: PolicyRule[] = [];
	for (const raw of v.rules) {
		if (typeof raw !== "object" || raw === null) return null;
		const r = raw as Record<string, unknown>;
		if (typeof r.id !== "string" || typeof r.command !== "string") return null;
		if (r.action !== "ask" && r.action !== "deny") return null;
		// accept both camelCase (argvAll) and snake_case (argv_all) so the rig-written JSON
		// dialect can be either without a breaking mismatch.
		const argvAll = r.argvAll ?? r.argv_all;
		const flagsAny = r.flagsAny ?? r.flags_any;
		if (argvAll !== undefined && !isStringArray(argvAll)) return null;
		if (flagsAny !== undefined && !isStringArray(flagsAny)) return null;
		rules.push({
			id: r.id,
			action: r.action,
			command: r.command,
			argvAll: argvAll as string[] | undefined,
			flagsAny: flagsAny as string[] | undefined,
			reason: typeof r.reason === "string" ? r.reason : "",
		});
	}
	const def: Decision = v.default === "ask" || v.default === "deny" ? v.default : "allow";
	const version = typeof v.version === "number" ? v.version : 1;
	return { version, default: def, rules };
}

function isStringArray(v: unknown): v is string[] {
	return Array.isArray(v) && v.every((x) => typeof x === "string");
}
