import { describe, it, before, after } from "node:test";
import * as assert from "node:assert";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { execSync } from "node:child_process";

import { findProjectRoot, getSanitizedEnv } from "../src/client.js";
import { SerializedQueue } from "../src/queue.js";
import { executeTodoDbTool, todoDbToolSchema } from "../src/tool.js";
import { renderToolResult } from "../src/render.js";

describe("Pi Adapter Core", () => {
  let tmpDir: string;

  before(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-adapter-test-"));
    execSync("git init --quiet", { cwd: tmpDir });
    execSync('git config user.name "Test"', { cwd: tmpDir });
    execSync('git config user.email "test@example.com"', { cwd: tmpDir });
    fs.mkdirSync(path.join(tmpDir, ".todo-db"), { recursive: true });
    fs.writeFileSync(
      path.join(tmpDir, ".todo-db", "config.json"),
      JSON.stringify({ project_id: "test-pi", repository: "todo-db" })
    );
  });

  after(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("discovers project root bounded by git root", () => {
    const subDir = path.join(tmpDir, "src", "nested");
    fs.mkdirSync(subDir, { recursive: true });
    assert.strictEqual(findProjectRoot(subDir), tmpDir);

    const nonProjectDir = fs.mkdtempSync(path.join(os.tmpdir(), "non-project-"));
    assert.strictEqual(findProjectRoot(nonProjectDir), null);
    fs.rmSync(nonProjectDir, { recursive: true, force: true });
  });

  it("sanitizes process environment", () => {
    process.env.TODO_DB_AUTH_TOKEN = "secret-auth-token";
    process.env.AWS_SECRET_ACCESS_KEY = "secret-aws-key";
    const clean = getSanitizedEnv();
    assert.strictEqual(clean.TODO_DB_AUTH_TOKEN, undefined);
    assert.strictEqual(clean.AWS_SECRET_ACCESS_KEY, undefined);
    delete process.env.TODO_DB_AUTH_TOKEN;
    delete process.env.AWS_SECRET_ACCESS_KEY;
  });

  it("serializes queue execution", async () => {
    const queue = new SerializedQueue();
    const order: number[] = [];

    const p1 = queue.enqueue(async () => {
      await new Promise((r) => setTimeout(r, 20));
      order.push(1);
    });
    const p2 = queue.enqueue(async () => {
      order.push(2);
    });

    await Promise.all([p1, p2]);
    assert.deepStrictEqual(order, [1, 2]);
  });

  it("validates todo_db tool schema", () => {
    assert.strictEqual(todoDbToolSchema.type, "object");
    assert.deepStrictEqual(todoDbToolSchema.required, ["action"]);
    assert.ok(todoDbToolSchema.properties.action.enum.includes("next"));
    assert.ok(todoDbToolSchema.properties.action.enum.includes("progress"));
    assert.ok(todoDbToolSchema.properties.action.enum.includes("finish"));
  });

  it("returns E_NO_PROJECT when project boundary is missing", async () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "empty-no-project-"));
    const res = await executeTodoDbTool({ action: "next" }, { cwd: emptyDir } as any);
    assert.strictEqual(res.details.error, "E_NO_PROJECT");
    fs.rmSync(emptyDir, { recursive: true, force: true });
  });

  it("renders custom tool results", () => {
    const mockTheme = {
      fg: (_col: string, t: string) => t,
      bg: (_col: string, t: string) => t,
      bold: (t: string) => t,
      dim: (t: string) => t,
    };

    const idleLines = renderToolResult({ details: { status: "idle" } }, { expanded: false, isPartial: false }, mockTheme);
    assert.ok(idleLines.some((l) => l.includes("idle")));

    const errorLines = renderToolResult({ details: { error: "Failed gate", code: "E_LINT_GATE" } }, { expanded: false, isPartial: false }, mockTheme);
    assert.ok(errorLines.some((l) => l.includes("E_LINT_GATE")));
  });
});
