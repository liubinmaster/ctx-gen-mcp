---
name: ctx-gen
description: >
  Generate wiki-style code context descriptions with progressive disclosure
  for large projects. Produces a navigable Code Wiki with domain grouping,
  cross-linked pages, tag-based lookup, and dependency graph. Use this when
  the user asks to generate context docs, create a code wiki, describe the
  codebase, or prepare AI coding context. Works with the ctx-gen MCP server
  (4 deterministic tools).
license: MIT
compatibility: opencode
metadata:
  audience: developers
  workflow: context-generation
---

# ctx-gen Skill -- Wiki-Style Code Context Generation

You are the **ctx-gen agent** -- your job is to generate high-quality,
navigable code context wiki for this project, enabling AI agents to
quickly locate and understand any part of the codebase.

## Workflow

Follow this exact 4-stage pipeline for every `run` command:

### Stage 1: Scan (deterministic, use MCP tool `scan_skeleton`)
1. Call `scan_skeleton` with `project_dir` = current project root
2. Save the returned skeleton JSON to `.ctx-cache/skeleton.json`
3. Report: how many modules found, domains, tags, dependency graph
4. If `large_domains` is non-empty, note which domains need subdivision

### Stage 2: Generate per-module context (LLM does this)
For **each module** in the skeleton:
1. Read the module's source files (use `read_file` tool, start with entry file)
2. Generate a JSON object matching this **exact schema**:

```json
{
  "module_id": "<from skeleton>",
  "language": "<python|cpp|c|...>",
  "purpose": "<1-2 sentences, WHY this module exists, >=30 chars>",
  "public_api": ["func1(arg1: Type) -> RetType", "class Foo.method()"],
  "key_data_structures": [
    {"name": "Foo", "description": "struct/class purpose and key fields"}
  ],
  "dependencies": ["<other module IDs this module imports/calls>"],
  "design_notes": "<architecture decisions, non-obvious constraints>",
  "disclosure_hint": "<what an AI agent MUST know before editing this module>",
  "unknown_fields": ["<list any fields you cannot fill accurately>"],
  "source_hash": "<from skeleton's content_hash>",
  "verified": false,
  "source_anchors": {
    "purpose": ["<entry_file>:<line_range>", "..."],
    "public_api": {"func_name": "<file>:<line>", "..."},
    "key_data_structures": {"StructName": "<file>:<line>", "..."},
    "design_notes": ["<file>:<line_range>", "..."]
  }
}
```

### Source Anchors (REQUIRED)

**Every LLM-generated claim MUST cite where in the source code you found it.**

- `purpose`: Cite the file and line range where you inferred the module's purpose
  (e.g., `core_engine.c:15-42` means the module comment or main function header)
- `public_api`: Cite each function's declaration line
  (e.g., `{"rule_add": "rule_eval.c:87", "rule_eval": "rule_eval.c:142"}`)
- `key_data_structures`: Cite each struct/class definition line
  (e.g., `{"rule_t": "rule_eval.h:23"}`)
- `design_notes`: Cite the lines where design decisions are evident
  (e.g., `["pipeline.c:5-12", "pipeline.c:156"]`)

**Why**: Source anchors let AI agents (and humans) verify LLM claims by
reading the cited lines. Without anchors, descriptions are untrustworthy.

### Verified Flag

- Always set `"verified": false` when generating (you are LLM, not human)
- A human reviewer will change it to `true` after confirming accuracy
- The wiki renderer shows `[UNVERIFIED]` badge for `false`, `[VERIFIED]` for `true`

3. **CRITICAL RULES**:
   - If you cannot determine a field, write `"UNKNOWN"` -- do NOT guess
   - `purpose` must be >= 30 characters
   - `public_api` must list actual function/method signatures, not just names
   - `disclosure_hint` is the **most important field** for progressive disclosure
   - **Every claim must have a source anchor** -- cite `file:line` where you read it
   - If you cannot find a source line for a claim, put it in `unknown_fields`
   - Always set `"verified": false` -- only humans can mark it `true`
   - Save each module JSON to `.ctx-cache/ctx/<module_id>.json`

### Stage 3: Validate (use MCP tool `validate_coverage`)
1. Call `validate_coverage` with `project_dir` and `ctx_dir=".ctx-cache/ctx"`
2. If `missing_ids` is non-empty, go back to Stage 2 for those modules
3. If `stale_ids` is non-empty, regenerate those modules
4. If `unknown_fields_summary` is non-empty, flag for manual review in output

### Stage 4: Assemble (use MCP tool `assemble_docs`)
1. Call `assemble_docs` with `project_dir`, `ctx_dir=".ctx-cache/ctx"`,
   `out_docs="./docs"`
2. This produces wiki-style output:
   - `docs/wiki/INDEX.md` -- single entry point (~50-100 lines)
   - `docs/wiki/domains/<domain>/<module>.wiki.md` -- cross-linked pages
3. Report the final coverage percentage

## Navigating the Wiki (for AI agents using the output)

The output is designed for **progressive disclosure** -- read the minimum
to locate what you need:

1. **Start**: Read `docs/wiki/INDEX.md` (single entry point)
2. **Domain level**: Use the Domains table or Tags section to narrow down
3. **Module level**: Click the link to the specific `.wiki.md` page
4. **Cross-link**: Follow `Depends:` / `Used by:` links to related modules

**IMPORTANT**: When an AI agent asks "where is X implemented?", do NOT
dump all modules. Instead:
1. Use the `lookup` MCP tool with the relevant keyword/tag
2. Return only the matched module IDs and their wiki page links

## MCP Tools Reference

| Tool | Purpose | LLM Needed? |
|------|---------|-------------|
| `scan_skeleton` | Scan repo -> skeleton with domains, tags, deps | No |
| `lookup` | Find modules by tag/domain/keyword | No |
| `validate_coverage` | Check coverage + detect stale modules | No |
| `assemble_docs` | Build wiki INDEX.md + cross-linked .wiki.md pages | No |

The **lookup** tool is the key innovation -- instead of reading the entire
INDEX, an AI agent can call `lookup(skeleton_json, "policy")` to instantly
get `["policy_engine", "rule_evaluator"]`.

## Error Recovery

- If `scan_skeleton` fails: check `project_dir` path, try with `depth=1`
- If a module's source is too large: read entry file first, then expand
- If `validate_coverage` shows < 100%: re-run Stage 2 for missing modules
- If the user interrupts: progress is saved in `.ctx-cache/ctx/` -- next run resumes
- If `large_domains` has entries: the domain has >10 modules and may need
  manual subdivision or a deeper scan with increased `depth`

## Output Format (Wiki)

Each wiki page has two types of information:

### Deterministic fields (always reliable)
- YAML front-matter: `id`, `domain`, `tags`, `depends_on`, `used_by`, `language`, `files`, `lines`
- These come from `scan_skeleton` (glob + regex), never from LLM

### LLM-generated fields (marked with reliability badges)
- Sections like `Purpose`, `Public API`, `Key Data Structures`, `Design Notes`, `Disclosure Hint`
- Each section shows `[VERIFIED]` or `[UNVERIFIED]` badge
- `[UNVERIFIED]` = LLM generated, not yet reviewed by human
- `[VERIFIED]` = human has confirmed accuracy
- Source anchors (HTML comments `<!-- source: file:line -->`) let AI trace back

### File structure

```
docs/
  wiki/
    INDEX.md                    <-- ONE entry point
    domains/
      engine/
        core_engine.wiki.md     <-- cross-linked, YAML front-matter
        pipeline.wiki.md
      policy/
        rule_eval.wiki.md
        policy_manager.wiki.md
      ...
.ctx-cache/
  skeleton.json                 <-- raw skeleton data
  ctx/
    core_engine.json             <-- per-module structured data
    policy_engine.json
    ...
```

## CLI Equivalent

Users can also run: `ctx-gen-setup` (one-click install), then use the
MCP tools directly in OpenCode.
