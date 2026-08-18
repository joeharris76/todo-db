import * as fs from "node:fs";
import * as path from "node:path";
import { spawn } from "node:child_process";

export interface ExecResult {
  exitCode: number;
  stdout: string;
  stderr: string;
}

export function findProjectRoot(startDir: string): string | null {
  let probe = path.resolve(startDir);
  const home = process.env.HOME ? path.resolve(process.env.HOME) : null;

  while (true) {
    const configPath = path.join(probe, ".todo-db", "config.json");
    if (fs.existsSync(configPath)) {
      return probe;
    }
    const gitPath = path.join(probe, ".git");
    const isGit = fs.existsSync(gitPath);

    const parent = path.dirname(probe);
    if (parent === probe || isGit || (home && probe === home)) {
      if (fs.existsSync(configPath)) {
        return probe;
      }
      break;
    }
    probe = parent;
  }
  return null;
}

export function getSanitizedEnv(): NodeJS.ProcessEnv {
  const env = { ...process.env };
  const sensitiveKeys = [
    "TODO_DB_AUTH_TOKEN",
    "TODO_DB_RO_AUTH_TOKEN",
    "TODO_DB_URL",
    "TODO_DB_CONFIG",
    "TODO_DB_TOOL",
    "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN",
    "TURSO_AUTH_TOKEN",
    "DYLD_INSERT_LIBRARIES",
    "LD_PRELOAD",
  ];
  for (const key of sensitiveKeys) {
    delete env[key];
  }
  return env;
}

export async function runTodoDb(
  projectRoot: string,
  args: string[],
  options?: { timeoutMs?: number; signal?: AbortSignal; envOverride?: NodeJS.ProcessEnv }
): Promise<ExecResult> {
  const timeoutMs = options?.timeoutMs ?? 30000;
  const wrapperPath = path.join(projectRoot, "_project", "scripts", "todo");
  let cmd = "todo-db";
  let cmdArgs = args;

  if (fs.existsSync(wrapperPath)) {
    try {
      fs.accessSync(wrapperPath, fs.constants.X_OK);
      cmd = wrapperPath;
    } catch {
      cmd = "todo-db";
    }
  }

  return new Promise<ExecResult>((resolve, reject) => {
    const env = options?.envOverride ?? getSanitizedEnv();
    const proc = spawn(cmd, cmdArgs, {
      cwd: projectRoot,
      env,
      shell: false,
      signal: options?.signal,
    });

    let stdout = "";
    let stderr = "";
    const byteCap = 64 * 1024;

    const timer = setTimeout(() => {
      proc.kill("SIGTERM");
      reject(new Error(`Command timed out after ${timeoutMs}ms: ${cmd} ${cmdArgs.join(" ")}`));
    }, timeoutMs);

    proc.stdout?.on("data", (chunk: Buffer) => {
      if (stdout.length < byteCap) {
        stdout += chunk.toString("utf-8");
      }
    });

    proc.stderr?.on("data", (chunk: Buffer) => {
      if (stderr.length < byteCap) {
        stderr += chunk.toString("utf-8");
      }
    });

    proc.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });

    proc.on("close", (code) => {
      clearTimeout(timer);
      resolve({
        exitCode: code ?? 0,
        stdout: stdout.trim(),
        stderr: stderr.trim(),
      });
    });
  });
}
