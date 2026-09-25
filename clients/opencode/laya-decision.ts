import type { Plugin } from "@opencode-ai/plugin";

/**
 * Laya decision plugin for opencode (v2 hooks). Talks to the warm-resident
 * Laya daemon (server/laya_http_mcp.py, 127.0.0.1 only) over the
 * Jev-compatible question API (POST /v1/laya — the same handler as
 * /v1/systemone; laya-named modules per our naming rule, the wire shape is
 * the spec'd one: {state, model?, questions} -> {answers, usage}).
 *
 * mode "advisory" (default): on permission.ask, if Laya says block/ask while
 *   opencode is about to allow, downgrade to "ask" and show the reason — it
 *   can only RAISE caution, never silently auto-allow anything.
 * mode "enforce": additionally map Laya "block" -> deny (use with care).
 *
 * Options (opencode.json "options"):
 *   bridge   http://127.0.0.1:11270
 *   mode     advisory | enforce
 *   timeout  ms, default 3000 — on any bridge failure we NO-OP (the harness
 *            must never be blocked by the decision plugin).
 */
const LAYA: Plugin = async ({ options = {}, client }) => {
  const bridge = String(options.bridge ?? "http://127.0.0.1:11270");
  const mode = String(options.mode ?? "advisory");
  const timeout = Number(options.timeout ?? 3000);

  const DISPOSITION = {
    allow: "safe to run as-is",
    ask_user: "possible risk, confirm with the operator first",
    block: "refuse: destructive or policy-violating",
  };

  /** One guardrail question over the question API; null on any failure. */
  async function guard(command: string): Promise<any | null> {
    try {
      const r = await fetch(`${bridge}/v1/laya`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          state: `About to run: ${command}`,
          questions: {
            disposition: {
              type: "choice",
              instructions:
                "How should the harness treat the action in `state`?",
              criteria: DISPOSITION,
            },
          },
        }),
        signal: AbortSignal.timeout(timeout),
      });
      const j = await r.json();
      const a = j?.answers?.disposition;
      if (!a || typeof a.choice !== "string") return null;
      // Verdict rail mirrors server/engine.py GUARD_ACT_CONFIDENCE (0.7):
      // act only on allow at conf >= 0.7; ask_user/block/unsure -> confirm.
      // Keep IN SYNC — loosening either side is a safety change.
      const verdict =
        a.choice === "block" ? "deny"
        : a.choice === "ask_user" || a.confidence < 0.7 ? "confirm"
        : "allow";
      return { choice: a.choice, confidence: a.confidence, verdict };
    } catch {
      return null; // bridge down / slow -> no-op
    }
  }

  return {
    // Hard safety gate on shell-equivalent permissions (type "bash").
    // Raising caution only in advisory; enforce mode also allows deny.
    "permission.ask": async (perm, out) => {
      if (out.status === "deny") return; // never loosen an existing deny
      if (perm.type !== "bash") return;
      const cmd =
        (perm.metadata?.command as string) ?? perm.pattern?.toString() ?? perm.title;
      if (!cmd) return;
      const d = await guard(String(cmd).slice(0, 900));
      if (!d) return;
      if (d.verdict === "deny" && mode === "enforce") {
        out.status = "deny";
        console.log(`[laya] deny: ${cmd} (conf ${d.confidence})`);
      } else if (d.verdict !== "allow" && out.status === "allow") {
        out.status = "ask";
        console.log(
          `[laya] raised ${perm.type} to ask: ${d.choice} conf=${d.confidence}`,
        );
      }
    },

    // Advisory context after tool executions (zero enforcement, pure hint).
    "tool.execute.after": async (input, output) => {
      if (options.advise !== true) return;
      const text =
        typeof output.output === "string" ? output.output : JSON.stringify(input.args);
      const d = await guard(text.slice(0, 900));
      if (d && d.verdict !== "allow" && typeof output.output === "string") {
        output.output +=
          `\n\n[laya advisory: decision model says "${d.choice}" (conf ${d.confidence}) ` +
          `about this result — consider escalating before acting further.]`;
      }
    },
  };
};

export default LAYA;
