// Loaded by Apprentice's owned OpenCode server, not installed into user projects.
export default async function apprenticeGuardrails() {
  return {
    "chat.params": async () => {
      const response = await fetch(process.env.APPRENTICE_BUDGET_URL, {
        headers: { authorization: `Bearer ${process.env.APPRENTICE_BUDGET_TOKEN}` },
        signal: AbortSignal.timeout(15000),
      });
      if (!response.ok) throw new Error("Apprentice budget controller is unavailable.");
      const result = await response.json();
      if (!result.allowed) throw new Error(result.reason || "Apprentice stopped this task.");
    },
    "experimental.session.compacting": async (_input, output) => {
      output.context.push("Keep this internal checkpoint under 600 words. Preserve the actual user request, mode, constraints, confirmed findings with references, completed reads/ranges, changes and tests, and one next unfinished step. Do not copy source files or reasoning. Never prescribe repeating an entire truncated file read. If sufficient evidence exists, the next step is the final answer.");
    },
  };
}
