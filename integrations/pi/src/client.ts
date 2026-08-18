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
  const hostileKeys = [
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
  ];
  for (const key of hostileKeys) {
    delete env[key];
  }
  return env;
}

export function resolveWrapperPath(projectRoot: string): string | null {
  const candidates = [
    path.join(projectRoot, "_project", "scripts", "todo"),
    path.join(projectRoot, "scripts", "todo"),
    path.join(projectRoot, "todo"),
  ];

  const configPath = path.join(projectRoot, ".todo-db", "config.json");
  if (fs.existsSync(configPath)) {
    try {
      const cfg = JSON.parse(fs.readFileSync(configPath, "utf-8"));
      if (cfg.wrapper) {
        candidates.unshift(path.resolve(projectRoot, cfg.wrapper));
      }
    } catch {
      // Ignore JSON parse errors in config
    }
  }

  for (const cand of candidates) {
    if (fs.existsSync(cand)) {
      try {
        fs.accessSync(cand, fs.constants.X_OK);
        return cand;
      } catch {
        // Not executable
      }
    }
  }
  return null;
}

export async function runTodoDb(
  projectRoot: string,
  args: string[],
  options?: { timeoutMs?: number; signal?: AbortSignal; envOverride?: NodeJS.ProcessEnv }
): Promise<ExecResult> {
  const timeoutMs = options?.timeoutMs ?? 30000;
  const wrapper = resolveWrapperPath(projectRoot);
  const cmd = wrapper || "todo-db";
  const cmdArgs = args;

  return new Promise<ExecResult>((resolve, reject) => {
    let completed = false;
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
      if (!completed) {
        proc.kill("SIGTERM");
        const killTimer = setTimeout(() => {
          if (!completed) {
            proc.kill("SIGKILL");
          }
        }, 2000);
        killTimer.unref();
        completed = true;
        reject(new Error(`Command timed out after ${timeoutMs}ms: ${cmd} ${cmdArgs.join(" ")}`));
      }
    }, timeoutMs);

    proc.stdout?.on("data", (chunk: Buffer) => {
      const currentBytes = Buffer.byteLength(stdout, "utf-8");
      if (currentBytes < byteCap) {
        stdout += chunk.toString("utf-8");
      }
    });

    proc.stderr?.on("data", (chunk: Buffer) => {
      const currentBytes = Buffer.byteLength(stderr, "utf-8");
      if (currentBytes < byteCap) {
        stderr += chunk.toString("utf-8");
      }
    });

    proc.on("error", (err) => {
      if (!completed) {
        completed = true;
        clearTimeout(timer);
        reject(err);
      }
    });

    proc.on("close", (code) => {
      if (!completed) {
        completed = true;
        clearTimeout(timer);
        resolve({
          exitCode: code ?? 0,
          stdout: stdout.trim(),
          stderr: stderr.trim(),
        });
      }
    });
  });
}
