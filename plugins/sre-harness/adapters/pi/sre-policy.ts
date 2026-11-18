/**
 * sre-harness tool policy for Pi, when the chat gateway drives it.
 *
 * Pi has no permission system. When the sre-harness chat gateway runs Pi in RPC
 * mode, this extension sends every tool call to the gateway as a blocking
 * extension dialog and does what the gateway answers. The gateway holds the
 * whole policy (the same rules Claude Code gets, through a strict matcher) and
 * the approval flow; nothing is decided here.
 *
 * It fails closed: outside RPC mode, without an answer, with a cancelled
 * dialog or an answer it cannot read, the call is blocked.
 *
 * Load it after adapters/pi/sre-audit.ts, so an attempt is recorded before it
 * is judged.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// The dialog title the gateway's Pi driver recognises. Anything else is an ordinary prompt.
export const POLICY_TITLE = "sre-harness-policy/v1";

type Answer = { decision?: unknown; reason?: unknown };

function blocked(reason: string) {
  return { block: true, reason: `Refused by the sre-harness gateway: ${reason}` };
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event: any, ctx: any) => {
    if (ctx?.mode !== "rpc") {
      return blocked("this extension only runs under the chat gateway (Pi RPC mode)");
    }
    const request = JSON.stringify({
      toolCallId: String(event.toolCallId ?? ""),
      tool: String(event.toolName ?? ""),
      input: event.input ?? {},
      parentToolCallId: event.parentToolCallId ?? null,
    });
    let raw: unknown;
    try {
      // No timeout: an approval can take minutes, and the gateway expires its own requests.
      raw = await ctx.ui.input(POLICY_TITLE, request);
    } catch {
      return blocked("the gateway could not be asked");
    }
    if (typeof raw !== "string" || raw === "") {
      return blocked("no answer");
    }
    let answer: Answer;
    try {
      answer = JSON.parse(raw) as Answer;
    } catch {
      return blocked("an answer that could not be read");
    }
    if (answer.decision === "allow") {
      return undefined;
    }
    return blocked(typeof answer.reason === "string" && answer.reason ? answer.reason : "not allowed");
  });
}
