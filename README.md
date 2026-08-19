# coding-agent-cli

A very basic coding CLI harness, written directly against the Anthropic API -
no agent framework in between. The point isn't to be a usable alternative to
Claude Code; it's to see the whole mechanism that powers tools like it,
in about 200 lines you can actually read start to finish.

## What it actually is

Every coding agent - Claude Code included - is the same loop underneath:

1. Send the conversation + a list of tools to the model.
2. If the model asks to run a tool, run it yourself and hand back the result.
3. Repeat until the model has nothing left to ask for.

This repo is that loop, manually, with two tools:

- **`bash`** - runs a shell command in the project directory (Anthropic-defined tool, `bash_20250124`)
- **`str_replace_based_edit_tool`** - views/creates/edits files (Anthropic-defined tool, `text_editor_20250728`)

Both are *client-executed*: Claude only ever returns "please run X" - this
process is what actually touches your filesystem or shell. See
[`src/tools.ts`](./src/tools.ts) for the handlers and
[`src/index.ts`](./src/index.ts) for the loop.

## Safety model (read this before running it)

- **File edits are confined to the directory you launch it from.** Every
  `path` the model sends is resolved and checked against that root,
  including symlink escapes - anything outside it is refused.
- **Every bash command needs your interactive y/n approval first**, printed
  right before it runs. There's no command allowlist - an allowlist that
  blocked pipes/`&&`/etc. would defeat the point of a coding tool - so the
  approval prompt is the actual safety boundary here. Set
  `AUTO_APPROVE_BASH=true` in `.env` only in a directory you don't mind the
  model having unattended shell access to (e.g. a scratch repo).
- Nothing here is sandboxed (no container, no VM). Run it in a project you'd
  be comfortable making direct edits and running commands in yourself.

## Setup

```bash
npm install
cp .env.example .env   # then fill in ANTHROPIC_API_KEY
npm start
```

Requires Node 18+ (developed against Node 20).

## Usage

```
coding-agent-cli - basic coding harness (claude-opus-5)
project root: /path/you/launched/it/from
type a task, or "exit" to quit

> add a .gitignore for a node project

[str_replace_based_edit_tool] create .gitignore
Created .gitignore

Done - added a .gitignore covering node_modules, dist, and .env.
```

It runs from whatever directory you launch `npm start` in - that directory
*is* the project it can see and edit. Point it at a small test folder first.

## Config (`.env`)

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(required)* | Your Anthropic API key |
| `CLAUDE_MODEL` | `claude-opus-5` | Model to use |
| `MAX_TOKENS` | `8192` | Response token cap per turn |
| `AUTO_APPROVE_BASH` | `false` | Skip the y/n prompt before bash commands |

## What this deliberately leaves out

No conversation persistence across runs, no streaming token-by-token UI
polish, no sub-agents, no MCP tools, no context compaction for long
sessions, no test suite. Those are all real features of a production coding
agent - they're just a second project, once this loop makes sense. This one
is scoped to "can I build and understand the loop itself."

## License

MIT
