/**
 * Core type declarations for Pi extension API and todo-db adapter.
 */

export interface ExtensionContext {
  cwd: string;
  ui: {
    notify(message: string, type?: "info" | "warning" | "error"): void;
    confirm(title: string, message: string): Promise<boolean>;
    select(title: string, options: string[]): Promise<string | null>;
    input(title: string, placeholder?: string): Promise<string | null>;
    custom<T>(component: any): Promise<T>;
    setStatus(key: string, text: string | undefined): void;
    setWidget(key: string, lines: string[] | undefined): void;
  };
  session?: {
    id: string;
  };
}

export interface Theme {
  fg(color: string, text: string): string;
  bg(color: string, text: string): string;
  bold(text: string): string;
  dim(text: string): string;
}

export interface ExtensionAPI {
  on(event: string, handler: (event: any, ctx: ExtensionContext) => Promise<any> | any): void;
  registerTool(tool: {
    name: string;
    label: string;
    description: string;
    parameters: any;
    execute(toolCallId: string, params: any, signal?: AbortSignal, onUpdate?: (update: any) => void, ctx?: ExtensionContext): Promise<any>;
    renderResult?(result: any, options: { expanded: boolean; isPartial: boolean }, theme: Theme): string[] | { render(width: number): string[] };
  }): void;
  registerCommand(name: string, command: {
    description: string;
    handler: (args: string, ctx: ExtensionContext) => Promise<void>;
  }): void;
}

export type TodoDBAction =
  | "next"
  | "take"
  | "context"
  | "progress"
  | "finish"
  | "claims"
  | "adopt"
  | "release";

export interface TodoDBParams {
  action: TodoDBAction;
  id?: string;
  wid?: string;
  evidence?: string;
  notes?: string;
  claim_token?: string;
  fields?: string;
  unit_limit?: number;
  session?: string;
  worktree?: string;
  branch?: string;
  model_assert?: boolean;
  run_verifications?: boolean;
  pr?: number;
  override_verifications?: string;
}
