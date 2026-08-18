import { describe, it } from "node:test";
import * as assert from "node:assert";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { TodoPanelComponent, updateTodoStatusWidget } from "../src/ui.js";

describe("Pi UI Components", () => {
  const mockTheme = {
    fg: (_col: string, t: string) => t,
    bg: (_col: string, t: string) => t,
    bold: (t: string) => `*${t}*`,
    dim: (t: string) => `(${t})`,
  };

  it("renders idle panel state", () => {
    let closed = false;
    const panel = new TodoPanelComponent({ status: "idle" }, mockTheme, () => {
      closed = true;
    });

    const lines = panel.render(80);
    assert.ok(lines.some((l) => l.includes("todo-db Status Panel")));
    assert.ok(lines.some((l) => l.includes("No active claims or ready items")));

    panel.handleInput("q");
    assert.strictEqual(closed, true);
  });

  it("does not execute project wrappers before trust", async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "todo-db-untrusted-"));
    fs.mkdirSync(path.join(root, ".git"));
    fs.mkdirSync(path.join(root, ".todo-db"));
    fs.writeFileSync(path.join(root, ".todo-db", "config.json"), JSON.stringify({ project_id: "p", repository: "r" }));
    const marker = path.join(root, "executed");
    const wrapperDir = path.join(root, "_project", "scripts");
    fs.mkdirSync(wrapperDir, { recursive: true });
    fs.writeFileSync(path.join(wrapperDir, "todo"), `#!/bin/sh\ntouch '${marker}'\n`);
    fs.chmodSync(path.join(wrapperDir, "todo"), 0o755);
    await updateTodoStatusWidget({
      cwd: root,
      isProjectTrusted: () => false,
      ui: { setStatus: () => {}, setWidget: () => {} },
    } as any);
    assert.strictEqual(fs.existsSync(marker), false);
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("renders active claim panel with work units", () => {
    let closed = false;
    const itemData = {
      id: "item-ui-test",
      title: "UI Item",
      priority: "high",
      state: "active",
      claimed_by: "agent-1",
      work_units: [
        { id: "w0", summary: "Step 0", status: "done" },
        { id: "w1", summary: "Step 1", status: "pending" },
      ],
      next_action: {
        action: "progress",
        command: "todo agent progress item-ui-test w1",
      },
    };

    const panel = new TodoPanelComponent(itemData, mockTheme, () => {
      closed = true;
    });

    const lines = panel.render(80);
    assert.ok(lines.some((l) => l.includes("item-ui-test")));
    assert.ok(lines.some((l) => l.includes("Step 0") && l.includes("✓")));
    assert.ok(lines.some((l) => l.includes("Step 1") && l.includes("○")));
    assert.ok(lines.some((l) => l.includes("Next Action")));

    panel.handleInput("\u001b");
    assert.strictEqual(closed, true);
  });
});
