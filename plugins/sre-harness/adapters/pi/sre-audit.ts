/**
 * sre-harness audit capture for Pi.
 *
 * Feeds every tool call Pi makes to the plugin's own audit hook
 * (scripts/audit_hook.py), shaped like a Claude Code hook payload, so the same
 * code binds the session to an incident, redacts, hashes output and chains the
 * log. Records carry `agent: "pi"` and the model as provider/id.
 *
 * Like the Claude Code hook it records nothing outside an incident and never
 * blocks a tool call: every failure is swallowed.
 *
 * Install: add this file's absolute path to "extensions" in
 * ~/.pi/agent/settings.json. Set SRE_HARNESS_PLUGIN if the file is copied
 * away from the plugin.
 */
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Pi's built-in tools under the names the harness (and its mutation check) knows.
const TOOL_NAMES: Record<string, string> = {
  bash: "Bash",
  read: "Read",
  write: "Write",
  edit: "Edit",
  grep: "Grep",
  find: "Glob",
  ls: "LS",
};
const HOOK_TIMEOUT_MS = 5000;

function pluginRoot(): string {
  return process.env.SRE_HARNESS_PLUGIN || resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
}

function resultText(result: unknown): string {
  // A tool result is { content: [{ type: "text", text }, ...], details? }.
  const content = (result as { content?: unknown })?.content;
  if (!Array.isArray(content)) return typeof result === "string" ? result : JSON.stringify(result ?? "");
  return content
    .map((part) => (part && typeof part === "object" && "text" in part ? String((part as { text: unknown }).text) : ""))
    .join("\n");
}

export default function (pi: ExtensionAPI) {
  const hook = resolve(pluginRoot(), "scripts", "audit_hook.py");
  // The input as the tool was asked for, so the completion record pairs with its attempt exactly.
  const inputs = new Map<string, unknown>();

  function send(payload: Record<string, unknown>): void {
    try {
      spawnSync("python3", [hook], {
        input: JSON.stringify(payload),
        timeout: HOOK_TIMEOUT_MS,
        stdio: ["pipe", "ignore", "ignore"],
      });
    } catch {
      // Fails open, like the Claude Code hook: capture never stops a tool call.
    }
  }

  function common(ctx: any): Record<string, unknown> {
    const model = ctx?.model ? `${ctx.model.provider}/${ctx.model.id}` : undefined;
    return { session_id: `pi-${ctx.sessionManager.getSessionId()}`, agent: "pi", model };
  }

  pi.on("tool_call", (event: any, ctx: any) => {
    const input = JSON.parse(JSON.stringify(event.input ?? {}));
    inputs.set(event.toolCallId, input);
    send({
      ...common(ctx),
      hook_event_name: "PreToolUse",
      tool_name: TOOL_NAMES[event.toolName] ?? event.toolName,
      tool_input: input,
      tool_use_id: event.toolCallId,
    });
  });

  pi.on("tool_execution_end", (event: any, ctx: any) => {
    const input = inputs.get(event.toolCallId) ?? event.args ?? {};
    inputs.delete(event.toolCallId);
    send({
      ...common(ctx),
      hook_event_name: "PostToolUse",
      tool_name: TOOL_NAMES[event.toolName] ?? event.toolName,
      tool_input: input,
      tool_use_id: event.toolCallId,
      tool_response: { stdout: resultText(event.result), is_error: Boolean(event.isError) },
    });
  });
}
