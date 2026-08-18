import { executeTodoDbTool, todoDbToolSchema } from "./tool.js";
import { renderToolResult } from "./render.js";
import { findProjectRoot, runTodoDb } from "./client.js";
import { TodoPanelComponent, updateTodoStatusWidget } from "./ui.js";
import type { ExtensionAPI, ExtensionContext } from "./types.js";

export default function (pi: ExtensionAPI): void {
  // 1. Session Lifecycle hooks
  pi.on("session_start", async (_event, ctx) => {
    await updateTodoStatusWidget(ctx);
  });

  pi.on("session_switch", async (_event, ctx) => {
    ctx.ui.setStatus("todo-db", undefined);
    ctx.ui.setWidget("todo-db", undefined);
    await updateTodoStatusWidget(ctx);
  });

  pi.on("session_fork", async (_event, ctx) => {
    await updateTodoStatusWidget(ctx);
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    ctx.ui.setStatus("todo-db", undefined);
    ctx.ui.setWidget("todo-db", undefined);
  });

  // 2. Register the single todo_db tool
  pi.registerTool({
    name: "todo_db",
    label: "Todo Database",
    description:
      "Manage project lifecycle and task execution with todo-db. " +
      "Actions: 'next' (inspect next work), 'take' (claim ready item), " +
      "'context' (get bounded task projection), 'progress' (complete work unit), " +
      "'finish' (complete item with model assertion), 'claims' (list active claims), 'release' (release claim).",
    parameters: todoDbToolSchema,
    async execute(toolCallId, params, signal, _onUpdate, ctx) {
      const result = await executeTodoDbTool(params, ctx, signal);
      if (ctx) {
        // Refresh status widget on mutations
        await updateTodoStatusWidget(ctx);
      }
      return result;
    },
    renderResult(result, options, theme) {
      return renderToolResult(result, options, theme);
    },
  });

  // 3. Register user command /todo-db
  pi.registerCommand("todo-db", {
    description: "Open interactive todo-db status panel",
    handler: async (_args, ctx: ExtensionContext) => {
      const projectRoot = findProjectRoot(ctx.cwd);
      if (!projectRoot) {
        ctx.ui.notify("No .todo-db project found in repository", "warning");
        return;
      }

      const res = await runTodoDb(projectRoot, ["agent", "next"]);
      let data: any = { status: "idle" };
      if (res.exitCode === 0) {
        try {
          data = JSON.parse(res.stdout);
          if (data.status === "claimed" && data.item) {
            const ctxRes = await runTodoDb(projectRoot, ["agent", "context", data.item.id]);
            if (ctxRes.exitCode === 0) {
              data = JSON.parse(ctxRes.stdout);
            }
          }
        } catch {
          data = { output: res.stdout };
        }
      }

      await ctx.ui.custom((theme: any, onClose: () => void) => {
        return new TodoPanelComponent(data, theme, onClose);
      });
    },
  });
}
