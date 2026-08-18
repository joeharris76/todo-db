import type {
  ExtensionAPI,
  ExtensionCommandContext,
  ExtensionContext,
  Theme,
} from "@earendil-works/pi-coding-agent";

export type { ExtensionAPI, ExtensionCommandContext, ExtensionContext, Theme };
export type ThemeLike = Pick<Theme, "fg" | "bold">;

export type TodoDBAction =
  | "next"
  | "take"
  | "context"
  | "progress"
  | "finish"
  | "release";

export interface TodoDBParams {
  action: TodoDBAction;
  id?: string;
  wid?: string;
  evidence?: string;
  notes?: string;
  claim_token?: string;
  fields?: string;
  section?: "work_units" | "scope" | "preserves" | "anti_patterns" | "verifications" | "item_dependencies" | "open_deferrals" | "prior_art";
  cursor?: number;
  limit?: number;
  session?: string;
  pr?: number;
}
