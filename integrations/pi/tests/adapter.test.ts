import { describe, it, before, after } from "node:test";
import * as assert from "node:assert";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { execSync } from "node:child_process";

import { findProjectRoot, getSanitizedEnv, runTodoDb } from "../src/client.js";
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
    process.env.LD_PRELOAD = "/evil.so";
    process.env.DYLD_INSERT_LIBRARIES = "/evil.dylib";
    process.env.TODO_DB_AUTH_TOKEN = "valid-token";
    process.env.GITHUB_TOKEN = "must-not-pass";
    process.env.PI_SESSION_FILE = "/secret/session.jsonl";
    const clean = getSanitizedEnv();
    assert.strictEqual(clean.LD_PRELOAD, undefined);
    assert.strictEqual(clean.DYLD_INSERT_LIBRARIES, undefined);
    assert.strictEqual(clean.TODO_DB_AUTH_TOKEN, "valid-token");
    assert.strictEqual(clean.GITHUB_TOKEN, undefined);
    assert.strictEqual(clean.PI_SESSION_FILE, undefined);
    delete process.env.LD_PRELOAD;
    delete process.env.DYLD_INSERT_LIBRARIES;
    delete process.env.TODO_DB_AUTH_TOKEN;
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
    const variants = todoDbToolSchema.properties.action.anyOf.map((entry: any) => entry.const);
    assert.ok(variants.includes("next"));
    assert.ok(variants.includes("progress"));
    assert.ok(variants.includes("finish"));
    assert.ok(!variants.includes("adopt"));
  });

  it("returns E_NO_PROJECT when project boundary is missing", async () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), "empty-no-project-"));
    const res = await executeTodoDbTool(
      { action: "next" },
      { cwd: emptyDir, isProjectTrusted: () => true, sessionManager: { getSessionId: () => "s1" } } as any
    );
    assert.strictEqual(res.details.code, "E_NO_PROJECT");
    fs.rmSync(emptyDir, { recursive: true, force: true });
  });

  it("validates required action parameters before running CLI", async () => {
    // Progress missing wid/evidence
    const ctx = { cwd: tmpDir, isProjectTrusted: () => true, sessionManager: { getSessionId: () => "s1" } } as any;
    const pRes = await executeTodoDbTool({ action: "progress", id: "i1" }, ctx);
    assert.strictEqual(pRes.isError, true);
    assert.ok(pRes.details.error.includes("required"));

    // Finish missing id
    const fRes = await executeTodoDbTool({ action: "finish" }, ctx);
    assert.strictEqual(fRes.isError, true);

    const untrusted = await executeTodoDbTool({ action: "next" }, { cwd: tmpDir, isProjectTrusted: () => false } as any);
    assert.strictEqual(untrusted.details.code, "E_UNTRUSTED_PROJECT");
  });

  it("enforces exact subprocess byte bounds", async () => {
    const wrapperDir = path.join(tmpDir, "_project", "scripts");
    fs.mkdirSync(wrapperDir, { recursive: true });
    const wrapper = path.join(wrapperDir, "todo");
    fs.writeFileSync(wrapper, "#!/bin/sh\npython3 -c \"print('x' * 100000)\"\n");
    fs.chmodSync(wrapper, 0o755);
    const result = await runTodoDb(tmpDir, ["agent", "next"], { byteCap: 1024 });
    assert.strictEqual(result.stdoutTruncated, true);
    assert.ok(Buffer.byteLength(result.stdout) <= 1024);
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
