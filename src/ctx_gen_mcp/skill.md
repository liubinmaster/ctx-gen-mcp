---
name: ctx-gen
description: >
  Generate progressive-disclosure code context descriptions (L0/L1/L2/L3)
  for large projects. Use this when the user asks to generate context docs,
  create PROJECT_CONTEXT.md, describe the codebase, or prepare AI coding
  context. Works with the ctx-gen MCP server (3 deterministic tools).
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: context-generation
---

# ctx-gen Skill

You are the **ctx-gen agent** — your job is to generate high-quality,
progressive-disclosure code context descriptions for this project.

## Workflow

Follow this exact 4-stage pipeline for every `run` command:

### Stage 1: Scan (deterministic, use MCP tool `scan_skeleton`)
1. Call `scan_skeleton` with `project_dir` = current project root
2. Save the returned skeleton JSON to `.ctx-cache/skeleton.json`
3. Report: how many modules found, what languages

### Stage 2: Generate per-module context (LLM does this)
For **each module** in the skeleton:
1. Read the module's source files (use `read_file` tool)
2. Generate a JSON object matching this **exact schema** (invalid if fields missing):

```json
{
  "module_id": "<from skeleton>",
  "language": "<python|cpp|c|...>",
  "purpose": "<1-2 sentences, WHY this module exists>",
  "public_api": ["func1()", "func2()"],
  "key_data_structures": [
    {"name": "Foo", "description": "..."}
  ],
  "dependencies": ["<other module IDs>"],
  "design_notes": "<architecture decisions, non-obvious constraints>",
  "disclosure_hint": "<what an AI agent needs to know before editing this module>",
  "unknown_fields": ["<list any fields you cannot fill accurately>"],
  "source_hash": "<from skeleton's content_hash>"
}
```

3. **CRITICAL RULES**:
   - If you cannot determine a field from the provided files, write `"UNKNOWN"` — do NOT guess
   - `purpose` must be ≥ 30 characters
   - `public_api` must list actual function/method signatures, not just names
   - `disclosure_hint` is the most important field for progressive disclosure
   - Save each module's JSON to `.ctx-cache/ctx/<module_id>.json`

### Stage 3: Validate (use MCP tool `validate_coverage`)
1. Call `validate_coverage` with `project_dir` and `ctx_dir=".ctx-cache/ctx"`
2. If `missing_ids` is non-empty, go back to Stage 2 for those modules
3. If `stale_ids` is non-empty, regenerate those modules
4. If `unknown_fields_summary` is non-empty, flag for manual review in the output

### Stage 4: Assemble (use MCP tool `assemble_docs`)
1. Call `assemble_docs` with `project_dir`, `ctx_dir=".ctx-cache/ctx"`, `out_docs="./docs"`
2. Verify `PROJECT_CONTEXT.md` was created
3. Report the final coverage percentage

## Progressive Disclosure Design

The output docs follow a 4-layer hierarchy:

- **L0** (30 words max): What does this project do? (in `PROJECT_CONTEXT.md` header)
- **L1** (module map table): Which modules exist, what do they do? (in `PROJECT_CONTEXT.md`)
- **L2** (per-module MD file): Full context for ONE module (in `docs/modules/<id>.md`)
- **L3** (design decisions): WHY was it built this way? (in `PROJECT_CONTEXT.md` or per-module)

When an AI agent later asks "where is X implemented?", point to the L1 table.
When it asks "how do I modify Y?", point to the L2 detail page.

## Error Recovery

- If `scan_skeleton` fails: check `project_dir` path, try with `depth=1`
- If a module's source is too large to read at once: read entry file first, then expand
- If `validate_coverage` shows < 100%: re-run Stage 2 for missing modules
- If the user interrupts: save progress to `.ctx-cache/ctx/` — next run resumes from cache

## CLI Equivalent

Users can also run: `ctx-gen-setup` (one-click install), then use the MCP tools directly.
