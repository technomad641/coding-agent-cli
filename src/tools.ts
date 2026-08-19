// Client-side handlers for Anthropic's built-in bash and text-editor tools.
//
// Both are "schema-less" tools - Claude already knows their input shape,
// we just declare {type, name} and execute whatever tool_use.input it sends.
// See: https://platform.claude.com/docs/en/agents-and-tools/tool-use/bash-tool
//      https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool

import { exec } from "node:child_process";
import { existsSync, realpathSync } from "node:fs";
import { readFile, writeFile, mkdir, cp } from "node:fs/promises";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { promisify } from "node:util";
import readline from "node:readline/promises";
import type Anthropic from "@anthropic-ai/sdk";

const execAsync = promisify(exec);

/** The two Anthropic-defined tools this harness supports. No input_schema - it's built into the model. */
export const TOOLS: Anthropic.Messages.ToolUnion[] = [
  { type: "bash_20250124", name: "bash" },
  { type: "text_editor_20250728", name: "str_replace_based_edit_tool" },
];

// ---------------------------------------------------------------------------
// Path confinement - every file op is confined to `root` (the directory the
// CLI was launched from). The model's `path` input is untrusted: reject
// anything that escapes root via "..", an absolute path, or a symlink.
// ---------------------------------------------------------------------------

function resolveWithinRoot(root: string, rawPath: string): string {
  const candidate = isAbsolute(rawPath) ? resolve(rawPath) : resolve(root, rawPath);
  assertContained(root, candidate, rawPath);

  // Walk up to the nearest existing ancestor and resolve symlinks there too,
  // so a symlink inside root pointing outside root is still caught.
  let probe = candidate;
  while (!existsSync(probe)) {
    const parent = dirname(probe);
    if (parent === probe) break;
    probe = parent;
  }
  assertContained(root, realpathSync(probe), rawPath);

  return candidate;
}

function assertContained(root: string, target: string, rawPath: string): void {
  const rel = relative(root, target);
  if (rel.startsWith("..") || isAbsolute(rel)) {
    throw new Error(
      `Refusing to touch "${rawPath}" - it resolves outside the project root (${root}).`,
    );
  }
}

// ---------------------------------------------------------------------------
// Bash tool
// ---------------------------------------------------------------------------

// Shared with index.ts's REPL loop - only one readline interface may own stdin.
export const rl = readline.createInterface({ input: process.stdin, output: process.stdout });

async function confirmBash(command: string): Promise<boolean> {
  if (process.env.AUTO_APPROVE_BASH === "true") return true;
  const answer = await rl.question(`\n  run: ${command}\n  allow? [y/N] `);
  return answer.trim().toLowerCase() === "y";
}

export async function handleBash(input: Record<string, unknown>, root: string): Promise<string> {
  if (input.restart === true) {
    return "(nothing to restart - this harness runs each command fresh, no persistent shell session)";
  }

  const command = String(input.command ?? "");
  if (!command.trim()) return "Error: empty command";

  const approved = await confirmBash(command);
  if (!approved) return "Command declined by the user - not run.";

  try {
    const { stdout, stderr } = await execAsync(command, {
      cwd: root,
      timeout: 120_000,
      maxBuffer: 10 * 1024 * 1024,
    });
    const combined = [stdout, stderr].filter(Boolean).join("\n").trim();
    return combined || "(no output)";
  } catch (err) {
    const e = err as { stdout?: string; stderr?: string; message: string };
    return [e.stdout, e.stderr, e.message].filter(Boolean).join("\n").trim();
  }
}

// ---------------------------------------------------------------------------
// Text editor tool: view / create / str_replace / insert
// ---------------------------------------------------------------------------

export async function handleTextEditor(input: Record<string, unknown>, root: string): Promise<string> {
  const command = String(input.command ?? "");
  const rawPath = String(input.path ?? "");

  try {
    const path = resolveWithinRoot(root, rawPath);

    switch (command) {
      case "view": {
        const content = await readFile(path, "utf8");
        const lines = content.split("\n");
        const [start = 1, end = lines.length] = Array.isArray(input.view_range)
          ? (input.view_range as number[])
          : [1, lines.length];
        return lines
          .slice(start - 1, end)
          .map((line, i) => `${start + i}\t${line}`)
          .join("\n");
      }

      case "create": {
        if (existsSync(path)) {
          await cp(path, `${path}.bak`);
        }
        await mkdir(dirname(path), { recursive: true });
        await writeFile(path, String(input.file_text ?? ""), "utf8");
        return `Created ${rawPath}`;
      }

      case "str_replace": {
        const content = await readFile(path, "utf8");
        const oldStr = String(input.old_str ?? "");
        const occurrences = content.split(oldStr).length - 1;
        if (occurrences === 0) return `Error: old_str not found in ${rawPath}`;
        if (occurrences > 1) return `Error: old_str matches ${occurrences} times in ${rawPath} - must match exactly once`;
        const updated = content.replace(oldStr, String(input.new_str ?? ""));
        await writeFile(path, updated, "utf8");
        return `Edited ${rawPath}`;
      }

      case "insert": {
        const content = await readFile(path, "utf8");
        const lines = content.split("\n");
        const insertLine = Number(input.insert_line ?? 0);
        lines.splice(insertLine, 0, String(input.insert_text ?? ""));
        await writeFile(path, lines.join("\n"), "utf8");
        return `Inserted into ${rawPath} after line ${insertLine}`;
      }

      default:
        return `Error: unknown text editor command "${command}"`;
    }
  } catch (err) {
    return `Error: ${(err as Error).message}`;
  }
}
