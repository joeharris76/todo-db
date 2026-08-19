import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { spawn } from "node:child_process";

export interface ExecResult {
  exitCode: number;
  stdout: string;
  stderr: string;
  stdoutTruncated: boolean;
  stderrTruncated: boolean;
}

export function findProjectRoot(startDir: string): string | null {
  let probe = path.resolve(startDir);
  const home = process.env.HOME ? path.resolve(process.env.HOME) : null;
  while (true) {
    const configPath = path.join(probe, ".todo-db", "config.json");
    if (fs.existsSync(configPath)) {
      try {
        const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
        if (typeof config.project_id === "string" && typeof config.repository === "string") return probe;
      } catch {
        return null;
      }
    }
    const atBoundary = fs.existsSync(path.join(probe, ".git"));
    const parent = path.dirname(probe);
    if (parent === probe || atBoundary || (home && probe === home)) break;
    probe = parent;
  }
  return null;
}

export function stablePrincipal(): string {
  const configured = process.env.TODO_DB_PI_PRINCIPAL?.trim();
  if (configured) return configured;
  return `pi:${process.env.USER || process.env.LOGNAME || "user"}@${os.hostname()}`;
}

export function agentArgs(args: string[]): string[] {
  return ["--actor", stablePrincipal(), "agent", ...args];
}

export function getSanitizedEnv(): NodeJS.ProcessEnv {
  const allowed = new Set([
    "CI", "COLORTERM", "HOME", "LANG", "LC_ALL", "LOGNAME", "NO_COLOR", "PATH", "SHELL", "TERM",
    "TMPDIR", "USER", "UV_CACHE_DIR", "VIRTUAL_ENV", "TODO_DB_AUTH_TOKEN", "TODO_DB_RO_AUTH_TOKEN",
    // ADR 0005: todo-db resolves a credential from this command when none is injected.
    // Dropping it here would make the provider unreachable for Pi alone.
    "TODO_DB_CREDENTIAL_COMMAND",
    "TODO_DB_URL", "TODO_DB_PATH", "TODO_DB_CONFIG", "TODO_DB_TOOL", "TODO_DB_PROJECT_ID",
    "TODO_DB_REPOSITORY", "TODO_DB_PI_PRINCIPAL",
  ]);
  return Object.fromEntries(Object.entries(process.env).filter(([key]) => allowed.has(key)));
}

export function resolveWrapperPath(projectRoot: string): string | null {
  const candidates = [
    path.join(projectRoot, "_project", "scripts", "todo"),
    path.join(projectRoot, "scripts", "todo"),
    path.join(projectRoot, "todo"),
  ];
  const configPath = path.join(projectRoot, ".todo-db", "config.json");
  try {
    const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
    if (typeof config.wrapper === "string") candidates.unshift(path.resolve(projectRoot, config.wrapper));
  } catch {
    return null;
  }
  for (const candidate of candidates) {
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return candidate;
    } catch {
      // Continue to the next fixed candidate.
    }
  }
  return null;
}

function appendBounded(chunks: Buffer[], chunk: Buffer, used: number, cap: number): number {
  const remaining = cap - used;
  if (remaining > 0) chunks.push(chunk.subarray(0, remaining));
  return used + chunk.length;
}

export async function runTodoDb(
  projectRoot: string,
  args: string[],
  options?: { timeoutMs?: number; signal?: AbortSignal; envOverride?: NodeJS.ProcessEnv; byteCap?: number }
): Promise<ExecResult> {
  const timeoutMs = options?.timeoutMs ?? 30000;
  const byteCap = options?.byteCap ?? 64 * 1024;
  const wrapper = resolveWrapperPath(projectRoot);
  const command = wrapper || "todo-db";
  return new Promise<ExecResult>((resolve, reject) => {
    let completed = false;
    const processHandle = spawn(command, args, {
      cwd: projectRoot,
      env: options?.envOverride ?? getSanitizedEnv(),
      shell: false,
      signal: options?.signal,
    });
    const stdoutChunks: Buffer[] = [];
    const stderrChunks: Buffer[] = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    const timer = setTimeout(() => {
      if (completed) return;
      completed = true;
      processHandle.kill("SIGTERM");
      const killTimer = setTimeout(() => processHandle.kill("SIGKILL"), 2000);
      killTimer.unref();
      reject(new Error(`Command timed out after ${timeoutMs}ms: ${command}`));
    }, timeoutMs);

    processHandle.stdout?.on("data", (chunk: Buffer) => {
      stdoutBytes = appendBounded(stdoutChunks, chunk, stdoutBytes, byteCap);
    });
    processHandle.stderr?.on("data", (chunk: Buffer) => {
      stderrBytes = appendBounded(stderrChunks, chunk, stderrBytes, byteCap);
    });
    processHandle.on("error", (error) => {
      if (!completed) {
        completed = true;
        clearTimeout(timer);
        reject(error);
      }
    });
    processHandle.on("close", (code) => {
      if (completed) return;
      completed = true;
      clearTimeout(timer);
      resolve({
        exitCode: code ?? 1,
        stdout: Buffer.concat(stdoutChunks).toString("utf8").trim(),
        stderr: Buffer.concat(stderrChunks).toString("utf8").trim(),
        stdoutTruncated: stdoutBytes > byteCap,
        stderrTruncated: stderrBytes > byteCap,
      });
    });
  });
}
