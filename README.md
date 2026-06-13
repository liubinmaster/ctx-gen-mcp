# ctx-gen-mcp

Code context wiki generator -- MCP Server + OpenCode plugin for navigable,
progressive-disclosure code docs with domain grouping, tags, and dependency graph.

## What It Does

Generates a **navigable Code Wiki** for large projects, so AI coding agents can
quickly locate and understand any module without reading the entire codebase.

Instead of dumping flat documentation, ctx-gen produces:

- **INDEX.md** -- single entry point with domain table, tag index, and module list
- **Cross-linked wiki pages** -- each module has its own `.wiki.md` with YAML
  front-matter, summary, dependency links, and detailed content
- **Domain grouping** -- modules auto-grouped by directory structure
- **Tag-based lookup** -- find modules by language, architecture level, tech feature
- **Dependency graph** -- shallow `#include`/`import` analysis with cross-links

## Progressive Disclosure

The wiki is designed so AI agents read the **minimum** to locate what they need:

1. **INDEX.md** (~50-100 lines) -- scan domains and tags
2. **lookup MCP tool** -- find modules by keyword without reading the INDEX
3. **Module wiki page** -- full context for one module with cross-links to related modules
4. **Follow links** -- `Depends:` / `Used by:` links for impact analysis

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
2. Say: `"use the ctx-gen skill to generate context wiki"`
3. Or switch to the `ctx-gen` agent in the agent panel
4. The agent will: scan -> generate per-module JSON -> validate -> assemble wiki

### MCP Tools (any MCP-compatible agent)

The package exposes 4 deterministic MCP tools:

| Tool | What it does |
|------|-------------|
| `scan_skeleton` | Scan repo -> skeleton with domains, tags, dependency graph |
| `lookup` | Find modules by tag/domain/keyword (no need to read full INDEX) |
| `validate_coverage` | Check all modules have context, detect stale ones |
| `assemble_docs` | Build wiki INDEX.md + cross-linked .wiki.md pages |

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
  skeleton.json             # repo structure with domains/tags/deps (deterministic)
  ctx/
    <module_id>.json       # per-module structured context
docs/
  wiki/
    INDEX.md               # single entry point
    domains/
      <domain>/
        <module>.wiki.md   # cross-linked per-module wiki page
```

Add these to `.gitignore`:
```
.ctx-cache/
docs/wiki/
```

## Architecture

### Core Insight: Separate Deterministic from LLM Operations

| Operation | Who does it | Why |
|-----------|-------------|-----|
| Repo scanning + domain grouping | `scan_skeleton` (deterministic) | Glob + regex never hallucinates |
| Module lookup by tag/keyword | `lookup` (deterministic) | String matching is exact |
| Per-module description | LLM (via Agent) | Needs semantic understanding |
| Coverage validation | `validate_coverage` (deterministic) | Hash comparison is exact |
| Wiki assembly | `assemble_docs` (deterministic) | Template + cross-link generation |

### Domain Grouping (Hybrid Strategy)

1. **Directory-based** first: `src/engine/` -> domain "engine"
2. If a domain has **>10 modules**, flagged for potential LLM subdivision
3. Domains are reflected in the output directory structure

### Tag Inference (Automatic)

Tags are inferred from file names, directory names, and shallow content analysis:

| Dimension | Examples | Detection Method |
|-----------|---------|-----------------|
| Language | `cpp`, `python`, `c` | File extension statistics |
| Architecture | `kernel-mode`, `user-mode`, `shared-lib` | Filename + content keywords |
| Tech feature | `driver`, `crypto`, `network`, `async`, `ipc` | Filename + content keywords |
| Build target | `static-lib`, `shared-lib`, `exe` | Build system analysis |

### Dependency Detection (Shallow)

Only direct `#include`, `import`, `require` statements are analyzed.
This covers ~80% of real dependencies with zero parser overhead.

## Requirements

- Python >= 3.10
- OpenCode >= 1.0 (for skill/agent support)
- Or any MCP-compatible agent (Claude Code, etc.)

## License

MIT
