# ctx-gen-mcp

Code context description generator — MCP Server + OpenCode plugin for progressive AI-friendly docs.

## What It Does

Generates **progressive-disclosure** code context docs (L0/L1/L2/L3) for large projects, so AI coding agents can understand your codebase without eating their entire context window.

- **L0**: One-liner — what does this project do?
- **L1**: Module map — which modules exist, what do they do?
- **L2**: Per-module detail — full context for ONE module
- **L3**: Architecture decisions — WHY was it built this way?

## One-Click Install

```bash
# 1. Install the pip package
pip install ctx-gen-mcp

# 2. Run one-click setup (installs skill + agent + MCP config)
ctx-gen-setup
```

That's it. OpenCode will now have:
- A `ctx-gen` skill (loadable via `/ctx-gen`)
- A `ctx-gen` agent (switchable in agent panel)
- MCP server config in `opencode.json`
- `AGENTS.md` in your project root

## Usage

### In OpenCode (recommended)

1. Open your project in OpenCode
2. Say: `"use the ctx-gen skill to generate context for this project"`
3. Or switch to the `ctx-gen` agent in the agent panel
4. The agent will: scan → generate per-module JSON → validate → assemble MD docs

### MCP Tools (any MCP-compatible agent)

The package exposes 3 deterministic MCP tools:

| Tool | What it does |
|------|-------------|
| `scan_skeleton` | Deterministic repo scan — no LLM needed |
| `validate_coverage` | Check all modules have context, detect stale ones |
| `assemble_docs` | Merge per-module JSONs into progressive MD docs |

### CLI

```bash
# Run MCP server directly (for testing)
ctx-gen-server

# Or:
python -m ctx_gen_mcp.server

# Re-run setup (e.g. after moving project)
ctx-gen-setup --project-dir /path/to/project

# Install globally (all projects)
ctx-gen-setup --global

# Uninstall
ctx-gen-setup --uninstall
```

## Output

After running, you'll have:

```
.ctx-cache/
  skeleton.json         # repo structure (deterministic)
  ctx/
    <module_id>.json   # per-module structured context
docs/
  PROJECT_CONTEXT.md  # L0 + L1 + L3 (main doc)
  modules/
    <module_id>.md     # L2 (per-module detail)
```

Add these to `.gitignore`:
```
.ctx-cache/
docs/PROJECT_CONTEXT.md
docs/modules/
```

## Requirements

- Python >= 3.10
- OpenCode >= 1.0 (for skill/agent support)
- Or any MCP-compatible agent (Claude Code, etc.)

## How It Works

The core insight: **separate deterministic operations from LLM operations**.

| Operation | Who does it | Why |
|-----------|-------------|-----|
| Repo scanning | `scan_skeleton` (deterministic) | Glob + regex never hallucinates |
| Per-module description | LLM (via Agent) | Needs semantic understanding |
| Coverage validation | `validate_coverage` (deterministic) | Hash comparison is exact |
| MD assembly | `assemble_docs` (deterministic) | Template filling, no creativity needed |

This separation is what makes the output **stable and coverage-guaranteed**.

## License

MIT
