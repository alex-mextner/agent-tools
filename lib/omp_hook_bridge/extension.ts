// agent-tools omp extension — bridges omp's native `pi.on("tool_call"/"tool_result", ...)`
// events into the shared agents-hooks/v1 contract by shelling out to the Python dispatcher
// in this same directory (`python3 -m omp_hook_bridge <event>`). See README.md for the full
// contract and, in particular, why this file's fail policy on `tool_call` is the OPPOSITE
// of the sibling opencode bridge's `plugin.js`.
//
// omp's extension loader imports this module with an `?mtime=<n>` cache-buster query string
// appended to the file URL (docs/extension-loading.md) so edited source reloads without a
// restart. `fileURLToPath` strips the query string when converting back to a filesystem
// path, so `realpathSync(fileURLToPath(import.meta.url))` below resolves correctly whether
// or not that cache-buster is present.

import { spawnSync } from "node:child_process";
import { realpathSync } from "node:fs";
import { delimiter, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const extensionPath = realpathSync(fileURLToPath(import.meta.url));
const libDir = resolve(dirname(extensionPath), "..");
// This bounds the WHOLE dispatch — every matching descriptor run serially for every
// fanned-out file path — not one descriptor. It must exceed the longest shipped descriptor
// budget (960,000 ms: every hatch-capable descriptor, e.g. block-raw-pr-merge /
// worktree-only-writes / orchestrator-stays-thin, waits that long for a Telegram approval)
// with room for more than one such wait in a single call, because this bridge fails OPEN on
// a dispatcher failure (see `runBridge` below and README.md "Fail policy"): a timeout HERE
// is not the safe fallback it is for opencode's fail-CLOSED plugin — it converts a still-
// running fail-closed hook into an allow. 2,000,000 ms covers two serial full approval
// windows plus margin (a hatch wait only happens when the agent set a RIG_HATCH_REQUEST_*
// var, so two in one call is already the rare case); three or more in one call remain a
// documented residual. Override with OMP_HOOK_BRIDGE_TIMEOUT_MS.
const DEFAULT_DISPATCHER_TIMEOUT_MS = 2000000;
const DISPATCHER_TIMEOUT_MS = resolveDispatcherTimeout();

function resolveDispatcherTimeout(): number {
  const parsed = Number.parseInt(
    process.env.OMP_HOOK_BRIDGE_TIMEOUT_MS || String(DEFAULT_DISPATCHER_TIMEOUT_MS),
    10,
  );
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_DISPATCHER_TIMEOUT_MS;
}

interface BridgeResult {
  block: boolean;
  reason?: string;
}

// Deliberately the OPPOSITE of opencode's `plugin.js`, which THROWS on a `tool.execute.before`
// bridge failure to fail closed. Per docs/hooks.md, a THROWN error from a `tool_call` handler
// blocks the call in omp too — but omp is Alex's interactive daily driver, and a wedged/broken
// dispatcher process (missing python3, a bug in this bridge, a descriptor timeout) must never
// silently brick every bash/edit/write/task call in a live session. So bridge-infrastructure
// failure here always resolves to "no opinion" (return undefined => allow); only an EXPLICIT
// `{"decision":"block","reason":...}` from the dispatcher becomes a real block. See README.md
// "Fail policy" for the full reasoning.
function runBridge(
  eventName: "tool_call" | "tool_result",
  event: any,
  ctx: any,
  registration: number,
): BridgeResult | undefined {
  const python = process.env.OMP_HOOK_BRIDGE_PYTHON || "python3";
  const priorPythonPath = process.env.PYTHONPATH || "";
  let proc;
  try {
    const payload = JSON.stringify({
      event: eventName,
      toolName: event?.toolName,
      input: event?.input ?? {},
      cwd: ctx?.cwd || process.cwd(),
      toolCallId: event?.toolCallId,
      // TOP-LEVEL identity, computed from omp's own objects (never from `event.input`, which
      // the dispatcher strips of every agent-shaped key). See `subagentIdentity` below.
      ...subagentIdentity(registration, ctx),
    });
    proc = spawnSync(python, ["-m", "omp_hook_bridge", eventName], {
      input: payload,
      encoding: "utf8",
      timeout: DISPATCHER_TIMEOUT_MS,
      env: {
        ...process.env,
        PYTHONPATH: priorPythonPath ? `${libDir}${delimiter}${priorPythonPath}` : libDir,
      },
    });
  } catch (error: any) {
    console.error(`omp-hook-bridge: dispatcher setup failed, allowing call: ${error?.message}`);
    return undefined;
  }
  return interpretDispatcherResult(eventName, proc);
}

function interpretDispatcherResult(eventName: string, proc: any): BridgeResult | undefined {
  if (proc.error) {
    const label = proc.error.code === "ETIMEDOUT" ? "timed out" : "launch failed";
    console.error(`omp-hook-bridge[${eventName}]: dispatcher ${label}, allowing call: ${proc.error.message}`);
    return undefined;
  }
  if (proc.stderr) {
    process.stderr.write(proc.stderr);
  }
  if (proc.signal) {
    console.error(`omp-hook-bridge[${eventName}]: dispatcher terminated by ${proc.signal}, allowing call`);
    return undefined;
  }
  if (proc.status !== 0) {
    console.error(`omp-hook-bridge[${eventName}]: dispatcher exited ${proc.status}, allowing call`);
    return undefined;
  }
  const stdout = (proc.stdout || "").trim();
  if (!stdout) {
    return undefined;
  }
  let result: any;
  try {
    result = JSON.parse(stdout);
  } catch (error: any) {
    console.error(`omp-hook-bridge[${eventName}]: invalid dispatcher JSON, allowing call: ${error?.message}`);
    return undefined;
  }
  if (result?.decision === "block") {
    return { block: true, reason: result.reason || "blocked by agents-hooks/v1" };
  }
  return undefined;
}

// ── Subagent identity (agent-tools#573) ────────────────────────────────────────────────────
//
// omp runs a `task` subagent IN THIS PROCESS: it creates a child agent session and invokes every
// extension's default export ONCE MORE for it, with a fresh `pi` (captured live on omp 18.0.11:
// the module loads once, the export runs twice, the child's `tool_call`s arrive on the second
// registration's handler). The child's `sessionManager` reports a different session id and a
// CHILD-SHAPED session file — nested under the parent session's stem directory
// (`<sessions>/<project>/<stamp>_<uuid>/<TaskName>.jsonl`) or, in `--no-session` mode, under an
// `omp-task-*` temp directory — whereas a root session's file is a top-level sibling
// (`<sessions>/<project>/<stamp>_<uuid>.jsonl`) or, with `--no-session`, absent.
//
// A tool call is tagged as a subagent's ONLY when BOTH hold: the registration is not the first
// one in this process AND its session file is child-shaped. The conjunction is what makes the
// root immune: an interactive `/new` (should it ever re-run the export) yields a fresh ROOT-shaped
// file, so the second signal fails and the root stays the orchestrator (fail closed in the relax
// direction). Everything read here is omp's own bookkeeping — the model cannot reach it from a
// tool call's arguments — so the dispatcher trusts the top-level `agentId`/`agentType` the way
// the Claude Code bridge trusts CC's top-level fields.
let registrations = 0;

const OMP_TASK_TEMP_DIR = /(^|\/)omp-task-[^/]+$/;
const OMP_SESSION_STEM_DIR = /(^|\/)\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z_[0-9a-f-]{36}$/;

function isChildSessionFile(file: unknown): boolean {
  if (typeof file !== "string" || !file) {
    return false;
  }
  const dir = file.slice(0, Math.max(file.lastIndexOf("/"), 0));
  return OMP_TASK_TEMP_DIR.test(dir) || OMP_SESSION_STEM_DIR.test(dir);
}

function subagentIdentity(registration: number, ctx: any): { agentId: string; agentType: string } | {} {
  if (registration <= 1) {
    return {};
  }
  let sessionId: unknown;
  let sessionFile: unknown;
  try {
    const sm = ctx?.sessionManager;
    sessionId = sm?.getSessionId?.();
    sessionFile = sm?.getSessionFile?.();
  } catch {
    return {};
  }
  if (!isChildSessionFile(sessionFile)) {
    return {};
  }
  const agentId = typeof sessionId === "string" && sessionId ? sessionId : `omp-task-${registration}`;
  return { agentId, agentType: "task" };
}

export default function (pi: any): void {
  const registration = ++registrations;
  pi.on("tool_call", async (event: any, ctx: any) => {
    try {
      return runBridge("tool_call", event, ctx, registration);
    } catch (error: any) {
      // Belt-and-braces: runBridge already catches internally, but a bridge-infrastructure
      // bug must NEVER escape as a throw here — that would block the tool call (see the
      // module-level comment on runBridge).
      console.error(`omp-hook-bridge: unexpected pre-call bridge error, allowing call: ${error?.message}`);
      return undefined;
    }
  });

  pi.on("tool_result", async (event: any, ctx: any) => {
    // Post-execution: the write already landed, so a block decision here is logged only
    // (matching opencode's `tool.execute.after` behavior), never surfaced as a real block.
    try {
      const result = runBridge("tool_result", event, ctx, registration);
      if (result?.block) {
        console.error(`omp-hook-bridge: post-write hook would have blocked; write already landed: ${result.reason}`);
      }
    } catch (error: any) {
      console.error(`omp-hook-bridge: unexpected post-call bridge error (ignored): ${error?.message}`);
    }
    return undefined;
  });
}
