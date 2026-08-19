// coding-agent-cli - a very basic coding CLI harness.
//
// This is the manual agentic loop, written out by hand instead of hidden
// behind a framework: send messages + tool definitions -> Claude replies,
// optionally asking to run a tool -> we execute it locally -> we send the
// result back -> repeat until Claude stops asking for tools. That's the
// entire mechanism behind Claude Code and every other coding agent; this
// harness just doesn't hide it.

import "dotenv/config";
import Anthropic from "@anthropic-ai/sdk";
import { rl, TOOLS, handleBash, handleTextEditor } from "./tools.js";

const apiKey = process.env.ANTHROPIC_API_KEY;
if (!apiKey) {
  console.error("Missing ANTHROPIC_API_KEY - copy .env.example to .env and set it.");
  process.exit(1);
}

const client = new Anthropic({ apiKey });
const MODEL = process.env.CLAUDE_MODEL ?? "claude-opus-5";
const MAX_TOKENS = Number(process.env.MAX_TOKENS ?? 8192);
const ROOT = process.cwd();

const SYSTEM_PROMPT = `You are a basic coding assistant running as a local CLI harness.
You have two tools: bash (runs shell commands) and a text editor (view/create/str_replace/insert).
Every file operation is confined to the project root: ${ROOT}
Every bash command requires the user's interactive y/n approval before it runs - if declined, adapt your plan instead of repeating the same command.
Be direct and concise. When a task is done, stop and summarize what changed instead of continuing to poke around.`;

const messages: Anthropic.MessageParam[] = [];

async function executeTool(name: string, input: Record<string, unknown>): Promise<string> {
  if (name === "bash") return handleBash(input, ROOT);
  if (name === "str_replace_based_edit_tool") return handleTextEditor(input, ROOT);
  return `Error: unknown tool "${name}"`;
}

function describeCall(name: string, input: Record<string, unknown>): string {
  if (name === "bash") return String(input.command ?? "");
  if (name === "str_replace_based_edit_tool") return `${input.command} ${input.path ?? ""}`;
  return JSON.stringify(input);
}

async function runTurn(userInput: string): Promise<void> {
  messages.push({ role: "user", content: userInput });

  while (true) {
    const stream = client.messages.stream({
      model: MODEL,
      max_tokens: MAX_TOKENS,
      system: SYSTEM_PROMPT,
      thinking: { type: "adaptive" },
      tools: TOOLS,
      messages,
    });

    stream.on("text", (delta) => process.stdout.write(delta));

    const message = await stream.finalMessage();
    console.log();

    if (message.stop_reason === "pause_turn") {
      messages.push({ role: "assistant", content: message.content });
      continue;
    }

    if (message.stop_reason === "refusal" && message.stop_details) {
      console.log(`[declined: ${message.stop_details.category ?? "policy"}]`);
    }
    if (message.stop_reason === "max_tokens") {
      console.log(`[cut off at MAX_TOKENS=${MAX_TOKENS} - raise it in .env for longer responses]`);
    }

    messages.push({ role: "assistant", content: message.content });

    const toolUseBlocks = message.content.filter(
      (b): b is Anthropic.ToolUseBlock => b.type === "tool_use",
    );

    if (toolUseBlocks.length === 0) break;

    const toolResults: Anthropic.ToolResultBlockParam[] = [];
    for (const block of toolUseBlocks) {
      const input = block.input as Record<string, unknown>;
      console.log(`\n[${block.name}] ${describeCall(block.name, input)}`);
      const result = await executeTool(block.name, input);
      console.log(result);
      toolResults.push({ type: "tool_result", tool_use_id: block.id, content: result });
    }

    messages.push({ role: "user", content: toolResults });
  }
}

async function main(): Promise<void> {
  console.log(`coding-agent-cli - basic coding harness (${MODEL})`);
  console.log(`project root: ${ROOT}`);
  console.log(`type a task, or "exit" to quit\n`);

  while (true) {
    const input = await rl.question("> ");
    const trimmed = input.trim();
    if (!trimmed) continue;
    if (trimmed === "exit" || trimmed === "quit") break;

    try {
      await runTurn(trimmed);
    } catch (err) {
      if (err instanceof Anthropic.AuthenticationError) {
        console.error("Authentication failed - check ANTHROPIC_API_KEY in .env.");
      } else if (err instanceof Anthropic.RateLimitError) {
        console.error("Rate limited - wait a moment and try again.");
      } else if (err instanceof Anthropic.APIError) {
        console.error(`API error: ${err.message}`);
      } else {
        console.error(`Error: ${(err as Error).message}`);
      }
    }
  }

  rl.close();
}

main();
