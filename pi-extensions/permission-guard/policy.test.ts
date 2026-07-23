/**
 * policy.test.ts — unit tests for the permission-guard matcher.
 * Run: `npm test` (in this dir) → `tsx --test policy.test.ts` (no pi runtime needed).
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import {
	coercePolicy,
	DEFAULT_POLICY,
	evaluateCommand,
	type Policy,
	splitSubCommands,
	tokenize,
} from "./policy.ts";

const P = DEFAULT_POLICY;
const decide = (cmd: string, policy: Policy = P) => evaluateCommand(cmd, policy).decision;

test("denies raw gh pr merge (flag-anywhere argv match)", () => {
	assert.equal(decide("gh pr merge 123"), "deny");
	assert.equal(decide("gh pr merge --admin 123 --squash"), "deny");
});

test("gh pr view / list / create stay allowed (only merge is denied)", () => {
	assert.equal(decide("gh pr view 123"), "allow");
	assert.equal(decide("gh pr list"), "allow");
	assert.equal(decide("gh pr create --fill"), "allow");
});

test("denies force-push in every position, flag-first and trailing", () => {
	assert.equal(decide("git push --force"), "deny");
	assert.equal(decide("git push origin main --force"), "deny");
	assert.equal(decide("git push -f origin main"), "deny");
	assert.equal(decide("git push origin main -f"), "deny");
});

test("--force-with-lease is NOT treated as a force-push deny (exact-token boundary)", () => {
	assert.equal(decide("git push --force-with-lease origin main"), "allow");
});

test("plain git push is allowed", () => {
	assert.equal(decide("git push origin main"), "allow");
});

test("denies --no-verify anywhere in the argv (commit OR push)", () => {
	assert.equal(decide("git commit --no-verify -m x"), "deny");
	assert.equal(decide('git commit -m "wip" --no-verify'), "deny");
	assert.equal(decide("git push --no-verify"), "deny");
});

test("a commit message that merely mentions --no-verify is NOT denied (it is quoted, one token)", () => {
	assert.equal(decide('git commit -m "mention --no-verify in text"'), "allow");
});

test("denies sudo rm; ask/allow other sudo", () => {
	assert.equal(decide("sudo rm -rf /tmp/x"), "deny");
	assert.equal(decide("sudo apt-get update"), "allow");
});

test("denies screencapture", () => {
	assert.equal(decide("screencapture -x out.png"), "deny");
	assert.equal(decide("/usr/sbin/screencapture out.png"), "deny");
});

test("asks before pkill / killall", () => {
	assert.equal(decide("pkill -9 node"), "ask");
	assert.equal(decide("killall Electron"), "ask");
});

test("asks before git reset --hard, allows soft/mixed reset", () => {
	assert.equal(decide("git reset --hard HEAD~1"), "ask");
	assert.equal(decide("git reset --soft HEAD~1"), "allow");
	assert.equal(decide("git reset HEAD file.txt"), "allow");
});

test("compound command takes the STRONGEST decision (deny > ask > allow)", () => {
	assert.equal(decide("git status && git push --force"), "deny");
	assert.equal(decide("echo hi | pkill node"), "ask");
	assert.equal(decide("ls; git reset --hard; echo done"), "ask");
	assert.equal(decide("git status && ls -la"), "allow");
});

test("basename resolution: absolute/relative paths to the binary still match", () => {
	assert.equal(decide("/usr/bin/git push --force"), "deny");
	assert.equal(decide("./git push -f"), "deny");
});

test("env VAR=val prefix does not hide the real argv0", () => {
	assert.equal(decide("env FOO=1 BAR=2 git push --force"), "deny");
});

test("bare inline VAR=val assignments do not hide the real argv0", () => {
	assert.equal(decide("FOO=1 git push --force"), "deny");
	assert.equal(decide("FOO=1 BAR=2 git push -f"), "deny");
	assert.equal(decide("FOO=1 env BAR=2 git push --force"), "deny");
});

test("env option flags do not hide the real argv0", () => {
	assert.equal(decide("env -i git push --force"), "deny");
	assert.equal(decide("env -C /tmp git push --force"), "deny");
	assert.equal(decide("env -u NAME git push -f"), "deny");
	assert.equal(decide("env -- git push --force"), "deny");
	assert.equal(decide("env -i FOO=1 git push --force"), "deny");
	// -S/--split-string's argument IS the command — it must stay visible as argv0, not be skipped
	assert.equal(decide("env -S git push --force"), "deny");
	// long options + short-flag clusters that take a separate argument
	assert.equal(decide("env --unset FOO git push --force"), "deny");
	assert.equal(decide("env --chdir /tmp git push --force"), "deny");
	assert.equal(decide("env --unset=FOO git push --force"), "deny");
	assert.equal(decide("env -iu NAME git push --force"), "deny");
	// optional-arg signal options only accept an ATTACHED value — a separate token is the command,
	// so it must NOT be swallowed (would otherwise be a deny bypass)
	assert.equal(decide("env --block-signal git push --force"), "deny");
	assert.equal(decide("env --ignore-signal git push --force"), "deny");
	assert.equal(decide("env --block-signal=INT git push --force"), "deny");
	// short-cluster getopt: an ATTACHED arg (`-uC` = `-u C`, `-uNAME`) must NOT swallow the command
	assert.equal(decide("env -uC git push --force"), "deny");
	assert.equal(decide("env -uNAME git push --force"), "deny");
	assert.equal(decide("env -iS git push --force"), "deny");
});

test("a `&` inside a redirection is not a clause separator (trailing flag still seen)", () => {
	assert.equal(decide("git push origin 2>&1 --force"), "deny");
	assert.equal(decide("git push --force >/dev/null 2>&1"), "deny");
	assert.equal(decide("git push origin &>log --force"), "deny");
});

test("a guarded flag glued to a redirect is still matched (redirect metachars are token boundaries)", () => {
	assert.equal(decide("git push --force&>out"), "deny");
	assert.equal(decide("git push --force>out"), "deny");
	assert.equal(decide("git push --force&>>out"), "deny");
});

test("a trailing CR (CRLF input) does not defeat exact-token flag matching", () => {
	assert.equal(decide("git push --force\r"), "deny");
	assert.equal(decide("git push --force\r\n"), "deny");
});

test("empty / whitespace command is allowed (nothing to guard)", () => {
	assert.equal(decide(""), "allow");
	assert.equal(decide("   "), "allow");
});

test("splitSubCommands ignores operators inside quotes", () => {
	assert.deepEqual(splitSubCommands('git commit -m "a; b && c"'), ['git commit -m "a; b && c"']);
	assert.deepEqual(splitSubCommands("a && b | c ; d"), ["a", "b", "c", "d"]);
});

test("tokenize strips surrounding quotes and splits on whitespace", () => {
	assert.deepEqual(tokenize('git commit -m "hello world"'), ["git", "commit", "-m", "hello world"]);
	assert.deepEqual(tokenize("git   push    --force"), ["git", "push", "--force"]);
});

test("evaluation reason + ruleId is surfaced for the winning decision", () => {
	const ev = evaluateCommand("git push --force", P);
	assert.equal(ev.decision, "deny");
	assert.equal(ev.ruleId, "git-force-push");
	assert.match(ev.reason ?? "", /force-push/i);
});

test("coercePolicy accepts a valid document, both camelCase and snake_case rule keys", () => {
	const camel = coercePolicy({
		version: 1,
		default: "allow",
		rules: [{ id: "r", action: "deny", command: "git", argvAll: ["push"], flagsAny: ["--force"], reason: "x" }],
	});
	assert.ok(camel);
	assert.equal(evaluateCommand("git push --force", camel!).decision, "deny");

	const snake = coercePolicy({
		version: 1,
		default: "allow",
		rules: [{ id: "r", action: "deny", command: "git", argv_all: ["push"], flags_any: ["--force"], reason: "x" }],
	});
	assert.ok(snake);
	assert.equal(evaluateCommand("git push --force", snake!).decision, "deny");
});

test("coercePolicy rejects malformed documents (caller then fails closed to DEFAULT_POLICY)", () => {
	assert.equal(coercePolicy(null), null);
	assert.equal(coercePolicy({}), null);
	assert.equal(coercePolicy({ rules: "nope" }), null);
	assert.equal(coercePolicy({ rules: [{ id: "x" }] }), null); // missing command/action
	assert.equal(coercePolicy({ rules: [{ id: "x", command: "git", action: "maybe" }] }), null);
	assert.equal(coercePolicy({ rules: [{ id: "x", command: "git", action: "deny", argvAll: ["push", 1] }] }), null);
});

test("sudo rm matches by basename even through an absolute path (sudo /bin/rm)", () => {
	assert.equal(decide("sudo /bin/rm -rf /tmp/x"), "deny");
	assert.equal(decide("sudo ./rm -rf /tmp/x"), "deny");
});

test("unquoted backslash-escaped flags still match (bash strips the backslash before argv)", () => {
	assert.equal(decide("git push --for\\ce"), "deny");
	assert.equal(decide("git commit -m x --no\\-verify"), "deny");
});

test("an escaped space stays inside one token, it is not a token boundary", () => {
	// `--for\ ce` is bash for the single argv token `--for ce` (one word, literal space) — it must
	// NOT weld/split into something that spuriously matches (or evades) an exact-token flag rule.
	assert.deepEqual(tokenize("git push --for\\ ce"), ["git", "push", "--for ce"]);
	assert.equal(decide("git push --for\\ ce"), "allow");
});

test("a trailing lone backslash at end of input is kept literally (no crash; under-match is harmless here since a real trailing backslash also breaks the command bash would execute)", () => {
	assert.deepEqual(tokenize("git push --force\\"), ["git", "push", "--force\\"]);
});

test("an escaped backslash collapses to one literal backslash; an escaped quote does not open a quote", () => {
	assert.deepEqual(tokenize("echo a\\\\b"), ["echo", "a\\b"]);
	assert.deepEqual(tokenize('echo a\\"b'), ["echo", 'a"b']);
});

test("sudo path-prefix basename matching over-matches (safe direction) when the binary name merely appears as an argument", () => {
	// Deliberate: ruleMatchesTokens scans every remaining token, not just the one actually
	// executed, so a path argument that happens to share a basename with a guarded subcommand also
	// trips the rule. This is the documented safe-over-match trade-off for a deny rule, pinned here
	// so a future edit that "fixes" this doesn't silently reopen the sudo/bin-path bypass instead.
	assert.equal(decide("sudo cp /bin/rm /tmp/rm.bak"), "deny");
});

test("coercePolicy rejects an unknown/typo'd default instead of silently coercing to allow", () => {
	assert.equal(coercePolicy({ version: 1, default: "dney", rules: [] }), null);
	assert.equal(coercePolicy({ version: 1, default: "DENY", rules: [] }), null);
	assert.equal(coercePolicy({ version: 1, default: 1, rules: [] }), null);
	assert.equal(coercePolicy({ version: 1, default: true, rules: [] }), null);
	assert.equal(coercePolicy({ version: 1, default: {}, rules: [] }), null);
	// every valid default value, and an absent default, are all still accepted
	assert.deepEqual(coercePolicy({ version: 1, default: "deny", rules: [] }), {
		version: 1,
		default: "deny",
		rules: [],
	});
	assert.deepEqual(coercePolicy({ version: 1, default: "ask", rules: [] }), {
		version: 1,
		default: "ask",
		rules: [],
	});
	assert.deepEqual(coercePolicy({ version: 1, rules: [] }), { version: 1, default: "allow", rules: [] });
	// a JSON `null` default (e.g. a generator emitting null for an unset field) is treated the same
	// as absent, NOT as an invalid value that discards the whole document
	assert.deepEqual(coercePolicy({ version: 1, default: null, rules: [] }), {
		version: 1,
		default: "allow",
		rules: [],
	});
});

test("a custom policy default of deny blocks unmatched commands", () => {
	const strict: Policy = { version: 1, default: "deny", rules: [] };
	assert.equal(decide("ls -la", strict), "deny");
});
