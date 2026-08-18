import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";
import { agentArgs, findProjectRoot, runTodoDb } from "./client.js";
import { renderToolResult } from "./render.js";
import { executeTodoDbTool, todoDbToolSchema } from "./tool.js";
import { TodoPanelComponent, updateTodoStatusWidget } from "./ui.js";

export default function (pi: ExtensionAPI): void {
  let toolRegistered = false;
  const registerToolIfTrusted = (ctx: Parameters<typeof updateTodoStatusWidget>[0]) => {
    if (toolRegistered || !ctx.isProjectTrusted() || !findProjectRoot(ctx.cwd)) return;
    toolRegistered = true;
    pi.registerTool({
      name: "todo_db",
      label: "Todo Database",
      description: "Use one bounded, claim-coordinated todo-db workflow tool. Finish never executes stored shell commands.",
      parameters: todoDbToolSchema,
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        const result = await executeTodoDbTool(params, ctx, signal);
        if (["take", "progress", "finish", "release"].includes(params.action) && !result.isError) {
          await updateTodoStatusWidget(ctx);
        }
        return result;
      },
      renderResult(result, options, theme) {
        const lines = renderToolResult(result, options, theme);
        return { render: () => lines, invalidate: () => {} };
      },
    });
  };

  pi.on("session_start", async (_event, ctx) => {
    registerToolIfTrusted(ctx);
    await updateTodoStatusWidget(ctx);
  });
  pi.on("session_before_switch", async (_event, ctx) => {
    ctx.ui.setStatus("todo-db", undefined);
    ctx.ui.setWidget("todo-db", undefined);
  });
  pi.on("session_before_fork", async (_event, ctx) => {
    ctx.ui.setStatus("todo-db", undefined);
    ctx.ui.setWidget("todo-db", undefined);
  });
  pi.on("session_shutdown", async (_event, ctx) => {
    ctx.ui.setStatus("todo-db", undefined);
    ctx.ui.setWidget("todo-db", undefined);
  });

  pi.registerCommand("todo-db", {
    description: "Open the read-only todo-db status panel",
    handler: async (_args, ctx: ExtensionCommandContext) => {
      if (!ctx.isProjectTrusted()) {
        ctx.ui.notify("todo-db is disabled until this project is trusted", "warning");
        return;
      }
      if (ctx.mode !== "tui") {
        ctx.ui.notify("/todo-db panel is available only in TUI mode", "warning");
        return;
      }
      const projectRoot = findProjectRoot(ctx.cwd);
      if (!projectRoot) {
        ctx.ui.notify("No valid .todo-db project found", "warning");
        return;
      }
      const result = await runTodoDb(projectRoot, agentArgs(["next"]));
      let data: any = { status: "idle" };
      if (result.exitCode === 0 && !result.stdoutTruncated) {
        try {
          data = JSON.parse(result.stdout);
          if (data.status === "claimed" && data.item) {
            const contextResult = await runTodoDb(projectRoot, agentArgs(["context", data.item.id, "--limit", "20"]));
            if (contextResult.exitCode === 0 && !contextResult.stdoutTruncated) data = JSON.parse(contextResult.stdout);
          }
        } catch {
          data = { status: "error", error: "Invalid todo-db protocol output" };
        }
      }
      await ctx.ui.custom((_tui, theme, _keybindings, done) =>
        new TodoPanelComponent(data, theme, () => done(undefined))
      );
    },
  });
}
