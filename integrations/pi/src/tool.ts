import { Type } from "typebox";
import { agentArgs, findProjectRoot, runTodoDb } from "./client.js";
import { SerializedQueue } from "./queue.js";
import type { ExtensionContext, TodoDBParams } from "./types.js";

const Section = Type.Union([
  Type.Literal("work_units"), Type.Literal("scope"), Type.Literal("preserves"), Type.Literal("anti_patterns"),
  Type.Literal("verifications"), Type.Literal("item_dependencies"), Type.Literal("open_deferrals"), Type.Literal("prior_art"),
]);

export const todoDbToolSchema = Type.Object({
  action: Type.Union([
    Type.Literal("next"), Type.Literal("take"), Type.Literal("context"),
    Type.Literal("progress"), Type.Literal("finish"), Type.Literal("release"),
  ], { description: "Lifecycle action" }),
  id: Type.Optional(Type.String({ description: "Target item identifier" })),
  wid: Type.Optional(Type.String({ description: "Work unit identifier" })),
  evidence: Type.Optional(Type.String({ description: "Evidence for progress" })),
  notes: Type.Optional(Type.String({ description: "Optional progress notes" })),
  claim_token: Type.Optional(Type.String({ description: "Current claim generation token" })),
  fields: Type.Optional(Type.String({ description: "Comma-separated context fields" })),
  section: Type.Optional(Section),
  cursor: Type.Optional(Type.Integer({ minimum: 0 })),
  limit: Type.Optional(Type.Integer({ minimum: 0, maximum: 100 })),
  pr: Type.Optional(Type.Integer({ minimum: 1 })),
}, { additionalProperties: false });

const mutationQueue = new SerializedQueue();

function toolError(code: string, error: string): any {
  const details = { code, error };
  return { content: [{ type: "text", text: JSON.stringify(details) }], details, isError: true };
}

export async function executeTodoDbTool(
  params: TodoDBParams,
  ctx?: ExtensionContext,
  signal?: AbortSignal
): Promise<any> {
  if (!ctx?.isProjectTrusted()) return toolError("E_UNTRUSTED_PROJECT", "todo-db is disabled until this project is trusted");
  const projectRoot = findProjectRoot(ctx.cwd);
  if (!projectRoot) return toolError("E_NO_PROJECT", "No valid .todo-db project boundary was discovered");
  const isMutation = ["take", "progress", "finish", "release"].includes(params.action);

  const runAction = async () => {
    const args: string[] = [params.action];
    if (params.action === "take") {
      if (params.id) args.push(params.id);
      args.push("--session", ctx.sessionManager.getSessionId());
    } else if (params.action === "context") {
      if (!params.id) return toolError("E_ARGUMENT", "id is required for context");
      args.push(params.id);
      if (params.fields) args.push("--fields", params.fields);
      if (params.section) args.push("--section", params.section);
      if (params.cursor !== undefined) args.push("--cursor", String(params.cursor));
      if (params.limit !== undefined) args.push("--limit", String(params.limit));
    } else if (params.action === "progress") {
      if (!params.id || !params.wid || !params.evidence || !params.claim_token) {
        return toolError("E_ARGUMENT", "id, wid, evidence, and claim_token are required for progress");
      }
      args.push(params.id, params.wid, "--evidence", params.evidence, "--claim-token", params.claim_token);
      if (params.notes) args.push("--notes", params.notes);
    } else if (params.action === "finish") {
      if (!params.id || !params.claim_token) return toolError("E_ARGUMENT", "id and claim_token are required for finish");
      args.push(params.id, "--claim-token", params.claim_token, "--model-assert");
      if (params.pr !== undefined) args.push("--pr", String(params.pr));
    } else if (params.action === "release") {
      if (!params.id || !params.claim_token) return toolError("E_ARGUMENT", "id and claim_token are required for release");
      args.push(params.id, "--claim-token", params.claim_token);
    }

    try {
      const result = await runTodoDb(projectRoot, agentArgs(args), { signal, byteCap: 16 * 1024 });
      if (result.stdoutTruncated || result.stderrTruncated) {
        return toolError("E_OUTPUT_TRUNCATED", "todo-db exceeded the bounded adapter output; request a smaller page");
      }
      if (result.exitCode === 0) {
        try {
          const details = JSON.parse(result.stdout);
          return { content: [{ type: "text", text: JSON.stringify(details) }], details };
        } catch {
          return toolError("E_PROTOCOL", "todo-db returned non-JSON output");
        }
      }
      try {
        const details = JSON.parse(result.stderr || result.stdout);
        const gate = result.exitCode === 1;
        return { content: [{ type: "text", text: JSON.stringify(details) }], details, isError: !gate };
      } catch {
        return toolError("E_PROTOCOL", result.stderr || result.stdout || `todo-db exited ${result.exitCode}`);
      }
    } catch (error: any) {
      return toolError("E_SUBPROCESS", error.message);
    }
  };

  return isMutation ? mutationQueue.enqueue(runAction) : runAction();
}
