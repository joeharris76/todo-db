import { findProjectRoot, runTodoDb } from "./client.js";
import { SerializedQueue } from "./queue.js";
import type { ExtensionContext, TodoDBParams } from "./types.js";

export const todoDbToolSchema = {
  type: "object",
  properties: {
    action: {
      type: "string",
      enum: ["next", "take", "context", "progress", "finish", "claims", "adopt", "release"],
      description: "Lifecycle action to perform",
    },
    id: {
      type: "string",
      description: "Target item identifier",
    },
    wid: {
      type: "string",
      description: "Work unit identifier (e.g. w0)",
    },
    evidence: {
      type: "string",
      description: "Evidence of completed work unit (required for progress)",
    },
    notes: {
      type: "string",
      description: "Optional notes for work unit progress",
    },
    claim_token: {
      type: "string",
      description: "Claim generation token",
    },
    fields: {
      type: "string",
      description: "Comma-separated list of fields to project for context",
    },
    unit_limit: {
      type: "number",
      description: "Max work units to return in context",
    },
    session: {
      type: "string",
      description: "Session identifier for claim adoption",
    },
    model_assert: {
      type: "boolean",
      description: "Assert verifications passed for model finish (no shell commands executed)",
    },
    pr: {
      type: "number",
      description: "Pull request number upon completion",
    },
  },
  required: ["action"],
  additionalProperties: false,
};

const mutationQueue = new SerializedQueue();

export async function executeTodoDbTool(
  params: TodoDBParams,
  ctx?: ExtensionContext,
  signal?: AbortSignal
): Promise<any> {
  const cwd = ctx?.cwd || process.cwd();
  const projectRoot = findProjectRoot(cwd);

  if (!projectRoot) {
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(
            {
              error: "No .todo-db project boundary discovered in repository. Run todo-db init-project first.",
              code: "E_NO_PROJECT",
            },
            null,
            2
          ),
        },
      ],
      details: { error: "E_NO_PROJECT" },
    };
  }

  const isMutation = ["take", "progress", "finish", "adopt", "release"].includes(params.action);

  const runAction = async () => {
    const cliArgs = ["agent", params.action];

    if (params.id) cliArgs.push(params.id);
    if (params.wid) cliArgs.push(params.wid);
    if (params.evidence) {
      cliArgs.push("--evidence", params.evidence);
    }
    if (params.notes) {
      cliArgs.push("--notes", params.notes);
    }
    if (params.claim_token) {
      cliArgs.push("--claim-token", params.claim_token);
    }
    if (params.fields) {
      cliArgs.push("--fields", params.fields);
    }
    if (params.unit_limit !== undefined) {
      cliArgs.push("--unit-limit", String(params.unit_limit));
    }
    if (params.session || (params.action === "take" && ctx?.session?.id)) {
      cliArgs.push("--session", params.session || ctx!.session!.id);
    }
    if (params.model_assert || params.action === "finish") {
      cliArgs.push("--model-assert");
    }
    if (params.pr !== undefined) {
      cliArgs.push("--pr", String(params.pr));
    }

    try {
      const result = await runTodoDb(projectRoot, cliArgs, { signal });
      if (result.exitCode === 0) {
        let parsed: any;
        try {
          parsed = JSON.parse(result.stdout);
        } catch {
          parsed = { output: result.stdout };
        }
        return {
          content: [{ type: "text", text: result.stdout || JSON.stringify(parsed, null, 2) }],
          details: parsed,
        };
      }

      let errorObj: any;
      try {
        errorObj = JSON.parse(result.stderr || result.stdout);
      } catch {
        errorObj = {
          error: result.stderr || result.stdout,
          exitCode: result.exitCode,
        };
      }
      return {
        content: [{ type: "text", text: JSON.stringify(errorObj, null, 2) }],
        details: errorObj,
        isError: true,
      };
    } catch (err: any) {
      return {
        content: [{ type: "text", text: JSON.stringify({ error: err.message }, null, 2) }],
        details: { error: err.message },
        isError: true,
      };
    }
  };

  if (isMutation) {
    return mutationQueue.enqueue(runAction);
  }
  return runAction();
}
