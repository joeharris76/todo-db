import type { Theme } from "./types.js";

export function renderToolResult(
  result: any,
  options: { expanded: boolean; isPartial: boolean },
  theme: Theme
): string[] {
  const details = result.details || {};
  const lines: string[] = [];

  if (details.error) {
    lines.push(theme.fg("error", `✗ todo-db error [${details.code || "ERR"}]: ${details.error}`));
    return lines;
  }

  if (details.status === "idle") {
    lines.push(theme.fg("dim", "○ Queue is idle (no ready items)"));
    return lines;
  }

  if (details.status === "ready" && details.item) {
    lines.push(
      theme.fg("accent", `→ Ready: ${details.item.id}`) +
        ` [${details.item.priority}] - ${details.item.title}`
    );
    return lines;
  }

  if (details.id && details.title) {
    const claimTag = details.claimed_by ? ` (claimed by ${details.claimed_by})` : "";
    lines.push(
      theme.fg("accent", `== ${details.id}`) +
        ` [${details.priority}] ${details.title}${claimTag}`
    );
    if (details.work_units && Array.isArray(details.work_units)) {
      for (const u of details.work_units) {
        const check = u.status === "done" ? theme.fg("success", "✓") : theme.fg("dim", "○");
        lines.push(`  ${check} ${u.id}: ${u.summary}`);
      }
    }
    if (details.next_action) {
      lines.push(
        theme.fg("dim", `Next: ${details.next_action.action} (${details.next_action.command || ""})`)
      );
    }
    return lines;
  }

  if (Array.isArray(details)) {
    lines.push(theme.fg("accent", `Claims (${details.length}):`));
    for (const c of details) {
      lines.push(`  • ${c.id} [${c.priority}] ${c.title} (held by ${c.claimed_by})`);
    }
    return lines;
  }

  lines.push(theme.fg("dim", JSON.stringify(details)));
  return lines;
}
