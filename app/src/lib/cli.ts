// Allowlisted CLI tools Ada may run on this machine (Tauri `run_cli`).
//
// The Rust side owns the allowlist and never goes through a shell. This
// module is the typed frontend seam: call `runCli("googleapps", ["auth"])`
// rather than inventing another path to spawn processes.

import { invoke } from "@tauri-apps/api/core";

export type CliToolId = "googleapps" | "silkscreen" | "python" | "kicad-cli";

export interface CliToolInfo {
  id: CliToolId | string;
  available: boolean;
  detail: string;
}

export interface CliResult {
  tool: string;
  argv: string[];
  status: number;
  stdout: string;
  stderr: string;
}

export class CliError extends Error {
  readonly result?: CliResult;
  constructor(message: string, result?: CliResult) {
    super(message);
    this.name = "CliError";
    this.result = result;
  }
}

/** Which allowlisted tools this machine can actually run. */
export async function listCliTools(): Promise<CliToolInfo[]> {
  return invoke<CliToolInfo[]>("list_cli_tools");
}

/**
 * Run one allowlisted tool. Throws `CliError` when the process exits non-zero
 * or when Rust refuses the tool / times out.
 */
export async function runCli(
  tool: CliToolId | string,
  args: string[] = [],
  timeoutSecs?: number
): Promise<CliResult> {
  const result = await invoke<CliResult>("run_cli", {
    tool,
    args,
    timeoutSecs: timeoutSecs ?? null,
  });
  if (result.status !== 0) {
    const detail = (result.stderr || result.stdout || `exit ${result.status}`).trim();
    throw new CliError(`${tool} failed: ${detail}`, result);
  }
  return result;
}
