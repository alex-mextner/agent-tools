import { spawnSync } from "node:child_process";
import { realpathSync } from "node:fs";
import { delimiter, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const pluginPath = realpathSync(fileURLToPath(import.meta.url));
const libDir = resolve(dirname(pluginPath), "..");
const DEFAULT_DISPATCHER_TIMEOUT_MS = 1000000;
const DISPATCHER_TIMEOUT_MS = resolveDispatcherTimeout();

function resolveDispatcherTimeout() {
  const parsed = Number.parseInt(
    process.env.OPENCODE_HOOK_BRIDGE_TIMEOUT_MS || String(DEFAULT_DISPATCHER_TIMEOUT_MS),
    10,
  );
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_DISPATCHER_TIMEOUT_MS;
}

function failOnDispatcherTimeout(hook, detail) {
  if (hook === "tool.execute.before") {
    throw new Error(`opencode hook bridge dispatcher timed out before tool execution: ${detail}`);
  }
  console.error(`opencode-hook-bridge: dispatcher timed out after tool execution, allowing call: ${detail}`);
}

function failOnDispatcherSignal(hook, signal) {
  if (hook === "tool.execute.before") {
    throw new Error(`opencode hook bridge dispatcher terminated by ${signal} before tool execution`);
  }
  console.error(`opencode-hook-bridge: dispatcher terminated by ${signal} after tool execution, allowing call`);
}

// ── Subagent identity (agent-tools#573) ────────────────────────────────────────────────────
//
// opencode runs a `task` subagent as a CHILD SESSION in the same server process: the child's
// `session.created` event carries `info.parentID` (the dispatching session), and every
// `tool.execute.before` for the child's own tool calls carries the CHILD's `input.sessionID`
// (captured on opencode 1.18.20). Both are opencode's own bookkeeping — `input.sessionID` is the
// hook input, not the model-authored `output.args`, and `parentID` comes from the session store
// (via the `event` hook, or `client.session.get` when the plugin missed the creation event, e.g.
// a child that already existed when the plugin loaded). A tool call from a session that HAS a
// parent is tagged `agentId: <sessionID>, agentType: "task"` on the payload's TOP level; the
// dispatcher reads only that (never `output.args`, which it strips of every agent-shaped key).
// A root session (no parentID) gets nothing: fail closed in the relax direction.
const parentBySession = new Map();

function noteSession(info) {
  if (info && typeof info.id === "string" && info.id) {
    parentBySession.set(info.id, typeof info.parentID === "string" && info.parentID ? info.parentID : null);
  }
}

async function subagentIdentity(input, ctx) {
  const sessionID = input?.sessionID;
  if (typeof sessionID !== "string" || !sessionID) {
    return {};
  }
  let parent = parentBySession.get(sessionID);
  if (parent === undefined) {
    try {
      const reply = await ctx?.client?.session?.get?.({ path: { id: sessionID } });
      const info = reply?.data ?? reply;
      noteSession(info && typeof info === "object" ? { id: sessionID, parentID: info.parentID } : { id: sessionID });
      parent = parentBySession.get(sessionID) ?? null;
    } catch (error) {
      console.error(`opencode-hook-bridge: session lookup failed, treating ${sessionID} as a root session: ${error?.message}`);
      return {};
    }
  }
  return parent ? { agentId: sessionID, agentType: "task" } : {};
}

function runBridge(hook, input, output, ctx, identity = {}) {
  const python = process.env.OPENCODE_HOOK_BRIDGE_PYTHON || "python3";
  const priorPythonPath = process.env.PYTHONPATH || "";
  let proc;
  try {
    const payload = JSON.stringify({
      hook,
      input: input || {},
      output: output || {},
      cwd: input?.cwd || ctx.directory || ctx.worktree || process.cwd(),
      directory: ctx.directory,
      worktree: ctx.worktree,
      // TOP-LEVEL identity from opencode's own session bookkeeping (see subagentIdentity).
      ...identity,
    });
    proc = spawnSync(python, ["-m", "opencode_hook_bridge", hook], {
      input: payload,
      encoding: "utf8",
      timeout: DISPATCHER_TIMEOUT_MS,
      env: {
        ...process.env,
        PYTHONPATH: priorPythonPath ? `${libDir}${delimiter}${priorPythonPath}` : libDir,
      },
    });
  } catch (error) {
    console.error(`opencode-hook-bridge: dispatcher setup failed, allowing call: ${error.message}`);
    return;
  }

  if (proc.error) {
    if (proc.error.code === "ETIMEDOUT") {
      failOnDispatcherTimeout(hook, proc.error.message);
      return;
    }
    console.error(`opencode-hook-bridge: dispatcher launch failed, allowing call: ${proc.error.message}`);
    return;
  }
  if (proc.stderr) {
    process.stderr.write(proc.stderr);
  }
  if (proc.signal) {
    failOnDispatcherSignal(hook, proc.signal);
    return;
  }
  if (proc.status !== 0) {
    console.error(`opencode-hook-bridge: dispatcher exited ${proc.status}, allowing call`);
    return;
  }
  const stdout = (proc.stdout || "").trim();
  if (!stdout) {
    return;
  }
  let result;
  try {
    result = JSON.parse(stdout);
  } catch (error) {
    console.error(`opencode-hook-bridge: invalid dispatcher JSON, allowing call: ${error.message}`);
    return;
  }
  if (result?.decision === "block") {
    throw new Error(result.reason || "blocked by agents-hooks/v1");
  }
}

export const AgentToolsHookBridge = async (ctx = {}) => ({
  event: async ({ event } = {}) => {
    if (event && typeof event.type === "string" && event.type.startsWith("session.")) {
      noteSession(event.properties?.info);
    }
  },
  "tool.execute.before": async (input, output) =>
    runBridge("tool.execute.before", input, output, ctx, await subagentIdentity(input, ctx)),
  "tool.execute.after": async (input, output) => {
    try {
      runBridge("tool.execute.after", input, output, ctx, await subagentIdentity(input, ctx));
    } catch (error) {
      console.error(`opencode-hook-bridge: post-write hook reported after tool execution; logging as feedback because the write already landed: ${error.message}`);
    }
  },
});
