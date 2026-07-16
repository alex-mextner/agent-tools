/**
 * permission-guard — a pi coding-agent extension that enforces command permissions on the bash
 * tool, giving pi the same deny-dangerous / ask-risky belt the other harnesses get.
 *
 * Why this exists
 * ---------------
 * pi ships NO built-in permission system — it runs bash with the invoking user's privileges
 * (earendil-works/pi docs). Its extension API DOES let you intercept a tool call before execution
 * (`pi.on("tool_call")` → return `{ block: true }` to deny), which is the sanctioned way to add a
 * permission gate. rig provisions this extension into `~/.pi/agent/extensions/permission-guard/`
 * for `harness.kind: pi` and writes the policy it enforces to `~/.pi/agent/rig-permission-policy.json`.
 *
 * Behavior
 * --------
 *   deny  → block the command, report the reason to the agent.
 *   ask   → confirm via the UI; a non-"Yes" answer blocks. With no UI (`ctx.hasUI` false, i.e. a
 *           non-interactive run) an ask is BLOCKED — fail-closed, we cannot prompt.
 *   allow → let it run (return undefined).
 * The policy is loaded once at startup; a broken/absent policy file falls back to the baked
 * baseline (loader.ts), so the dangerous denies fire even with zero/corrupt config.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { loadPolicy } from "./loader.ts";
import { evaluateCommand } from "./policy.ts";

export default function permissionGuard(pi: ExtensionAPI) {
	const { policy, warning } = loadPolicy();
	if (warning) console.error(warning);

	pi.on("tool_call", async (event, ctx) => {
		if (event.toolName !== "bash") return undefined;
		const command = String((event.input as { command?: unknown }).command ?? "");
		if (!command.trim()) return undefined;

		const verdict = evaluateCommand(command, policy);
		if (verdict.decision === "allow") return undefined;

		const reason = verdict.reason || "blocked by rig permission-guard policy";

		if (verdict.decision === "deny") {
			return { block: true, reason };
		}

		// ask: confirm interactively; block when there is no UI to confirm through.
		if (!ctx.hasUI) {
			return { block: true, reason: `${reason} (blocked: no UI to confirm in non-interactive mode)` };
		}
		const choice = await ctx.ui.select(`⚠️ ${reason}\n\n  ${command}\n\nAllow?`, ["Yes", "No"]);
		if (choice !== "Yes") {
			return { block: true, reason: "Blocked by user" };
		}
		return undefined;
	});
}
