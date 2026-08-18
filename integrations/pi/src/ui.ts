import { findProjectRoot, runTodoDb } from "./client.js";
import type { ExtensionContext, Theme } from "./types.js";

export async function updateTodoStatusWidget(ctx: ExtensionContext): Promise<void> {
  const projectRoot = findProjectRoot(ctx.cwd);
  if (!projectRoot) {
    ctx.ui.setStatus("todo-db", undefined);
    ctx.ui.setWidget("todo-db", undefined);
    return;
  }

  try {
    const claimsRes = await runTodoDb(projectRoot, ["agent", "claims"]);
    if (claimsRes.exitCode !== 0) {
      return;
    }
    const claims: any[] = JSON.parse(claimsRes.stdout);

    if (claims.length === 0) {
      // Check ready queue
      const nextRes = await runTodoDb(projectRoot, ["agent", "next"]);
      if (nextRes.exitCode === 0) {
        const nextData = JSON.parse(nextRes.stdout);
        if (nextData.status === "ready" && nextData.item) {
          ctx.ui.setStatus("todo-db", `todo: ready ${nextData.item.id}`);
          ctx.ui.setWidget("todo-db", [
            `todo-db: Ready Queue`,
            `→ ${nextData.item.id} [${nextData.item.priority}] ${nextData.item.title}`,
          ]);
          return;
        }
      }
      ctx.ui.setStatus("todo-db", "todo: idle");
      ctx.ui.setWidget("todo-db", ["todo-db: Queue is idle"]);
      return;
    }

    if (claims.length === 1) {
      const c = claims[0];
      ctx.ui.setStatus("todo-db", `todo: ${c.id}`);
      ctx.ui.setWidget("todo-db", [
        `todo-db [Active Claim]`,
        `== ${c.id} [${c.priority}] ${c.title}`,
        `Claimed by ${c.claimed_by} (${c.claimed_branch || "main"})`,
      ]);
      return;
    }

    // Multiple claims detected -> conflict warning
    ctx.ui.setStatus("todo-db", `todo: ${claims.length} claims (conflict)`);
    ctx.ui.setWidget("todo-db", [
      `todo-db: ⚠️ Multiple active claims detected (${claims.length})`,
      ...claims.map((c) => `• ${c.id} (${c.claimed_by})`),
    ]);
  } catch {
    // UI failure does not affect core execution
  }
}

export class TodoPanelComponent {
  private data: any;
  private theme: Theme;
  private onClose: () => void;

  constructor(data: any, theme: Theme, onClose: () => void) {
    this.data = data;
    this.theme = theme;
    this.onClose = onClose;
  }

  handleInput(data: string): void {
    if (data === "\u001b" || data === "\u0003" || data === "q") {
      this.onClose();
    }
  }

  render(width: number): string[] {
    const lines: string[] = [];
    const th = this.theme;

    lines.push("");
    lines.push(th.bold(th.fg("accent", "=== todo-db Status Panel ===")));
    lines.push("");

    if (!this.data || this.data.status === "idle") {
      lines.push(th.fg("dim", "No active claims or ready items in current project."));
    } else if (this.data.id) {
      lines.push(th.bold(`${this.data.id} [${this.data.priority}] ${this.data.title}`));
      lines.push(`State: ${this.data.state} | Claimed: ${this.data.claimed_by || "-"}`);
      lines.push("");
      lines.push(th.fg("accent", "Work Units:"));
      for (const u of this.data.work_units || []) {
        const mark = u.status === "done" ? th.fg("success", "✓") : th.fg("dim", "○");
        lines.push(`  ${mark} ${u.id}: ${u.summary}`);
      }
      if (this.data.next_action) {
        lines.push("");
        lines.push(th.fg("dim", `Next Action: ${this.data.next_action.action} (${this.data.next_action.command})`));
      }
    } else {
      lines.push(JSON.stringify(this.data, null, 2));
    }

    lines.push("");
    lines.push(th.fg("dim", "Press Escape or 'q' to close panel"));
    lines.push("");

    return lines;
  }
}
