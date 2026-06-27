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

## CRITICAL: Error Handling (Fail-Fast -- DO NOT IGNORE)

**After EVERY MCP tool call, you MUST check the response:**

1. If the response contains `_fatal_errors` (non-empty list):
   → **STOP immediately**.  Do NOT continue to the next stage.
   → Show the user ALL errors in `_fatal_errors`.
   → Ask the user how to fix each error before proceeding.

2. If the response contains `status: "error"`:
   → **STOP immediately**.

3. If the response contains `glossary_errors` (non-empty):
   → Warn the user.  Ask if they want to delete `glossary.json` and re-run Stage 2.5.

4. If the MCP tool raises an error (FastMCP surfaces it as an error message):
   → **STOP immediately**.
   → Show the user the full error message (including "How to fix").
   → Do NOT guess or work around the error.

5. If the response contains `hallucination_warnings` (non-empty):
   → Review the warnings.  If severe, ask the user before proceeding.

**Never silently skip errors.  Never say "let me continue despite the error".
The goal is: if anything is unexpected, STOP and ASK.**

## Workflow

Follow this exact 4-stage pipeline for every `run` command:

### Stage 1: Scan (deterministic, use MCP tool `scan_skeleton`)
1. Call `scan_skeleton` with `project_dir` = current project root
2. Save the returned skeleton JSON to `.ctx-cache/skeleton.json`
3. Report: how many modules found, domains, tags, dependency graph
4. If `large_domains` is non-empty, note which domains need subdivision

### Using `lookup` (disambiguation)
When the user's request maps to multiple candidate modules, call `lookup` **with `ctx_dir=".ctx-cache/ctx"`** so it returns a `candidates[]` list with each module's `purpose` summary. Use the `purpose` text to judge which module is the correct target — do NOT guess based on module ID alone.

Example:
```
User: "add an interception rule"
Agent: lookup(query="rule", ctx_dir=".ctx-cache/ctx")
  → candidates: [
      {id:"rule_evaluator", purpose:"Parse and evaluate interception rules..."},
      {id:"rule_engine",    purpose:"Execute rule matching engine..."}
    ]
Agent: "rule_evaluator is the correct target (it parses rules)."
```

### Stage 2: Generate per-module context (LLM does this)

**CRITICAL: `module_id` MUST exactly match the `id` field in the skeleton.**
Do NOT invent module IDs from file names or function names.
The skeleton says `"id": "src"` → your JSON must have `"module_id": "src"`.
If you use a different name the wiki page will show empty content.


**Optional: Use Coraline MCP for precise code analysis**
If Coraline MCP tools (coraline_*) are available in your tool list, query
Coraline first to get precise function signatures and call graphs.
This significantly improves public_api, dependencies, and disclosure_hint accuracy.

Before processing each module, run these Coraline queries:
1. coraline_get_symbols_overview(file_path="<module_entry_file>")
   → get all symbols (functions, structs, classes) in the module
2. coraline_node(name="<func_name>", file="<file>") for each public function
   → get exact function signature (from source, not LLM inference)
3. coraline_callers(name="<func_name>", file="<file>") and
   coraline_callees(name="<func_name>", file="<file>")
   → get precise call graph for dependencies and disclosure_hint
4. coraline_search(query="<module_name>", kind="module")
   → verify module boundary

Use these facts to write ctx JSON fields. If Coraline is not available,
proceed with the 5-step reading protocol below.

For **each module** in the skeleton, follow this **5-step reading protocol**:

#### Step A — Identify entry point
Read the entry file first (from skeleton `entry` field).
If entry is empty, check for: `main.c`, `main.py`, `__init__.py`, `index.ts`, `mod.rs`.
If none, read the file with the most exported symbols (biggest .h/.pyi file).

#### Step B — Scan all headers / interfaces
For C/C++: read every `.h` / `.hpp` file in the module dir.
For Python: read `__init__.py` and any file with `class`/`def` at module level.
For TypeScript: read `index.ts` and `.d.ts` files.
**Goal**: Build a complete list of public functions/classes with their signatures.
This is the `public_api` field. Write EXACT signatures, not just names.
Bad: `"rule_add"` | Good: `"rule_add(name: str, pattern: str, action: int) -> int"`

#### Step C — Find the "why" (purpose)
Look for:
1. Top-of-file doc comments (/* Module: ... */, """ ... """, # ...)
2. README.md or DESIGN.md in the module directory
3. First function in entry file -- what does it initialize/start?
4. CMakeLists.txt / BUILD target description
Write `purpose` as 1-2 sentences answering: "This module exists to ___."
Minimum 50 chars. If you can't find a clear answer, say what the entry function does.

#### Step D — Find design constraints (disclosure_hint)
These are the things that will BREAK if an AI agent ignores them.
Look for:
1. Comments starting with "IMPORTANT:", "NOTE:", "WARNING:", "FIXME:", "HACK:"
2. Global state / singletons that must be initialized first
3. Thread safety notes, lock ordering requirements
4. Protocol/wire format constraints (magic numbers, version fields)
5. Memory ownership rules (caller vs callee frees)
Write `disclosure_hint` as: "Before editing, know: ___"
This is the MOST important field -- an AI agent reads it first before touching anything.

#### Step E — Trace key data structures
Find the main struct/class that this module centers around.
Look for: typedef struct, dataclass, Protocol class, interface declaration.
Write its name and a 1-sentence description of what it represents.

#### Step F — Acronym / Abbreviation Verification (CRITICAL, anti-hallucination)
Codebases often contain abbreviations (ALL_CAPS identifiers, short names like
`ctx`, `rcv`, `snd`, `pkt`, `hdr`, `mpls`, `irdp`). **LLMs tend to
confidently invent explanations for abbreviations they do not understand.**
Follow this protocol strictly:

**F1. Collect all abbreviations** in the module:
- ALL_CAPS identifiers: `MDL`, `IRP`, `SND_BUF`, `RCV_QUEUE`, `MAX_RETRIES`
- Short lowercase names: `ctx`, `pkt`, `hdr`, `req`, `rsp`, `cfg`, `impl`
- Macro names, enum prefixes, typedef shorthands

**F2. Search for evidence BEFORE explaining any abbreviation**:
For each abbreviation, search the codebase (use grep/read_file) for:
1. A comment that explains it: `/* MDL = Memory Descriptor List */`, `# IRP: I/O Request Packet`
2. A doc file: `docs/terminology.md`, `README`, `DESIGN.md` mentioning it
3. A string constant or `#define` that expands it
4. A variable/struct name that is the "expanded form": e.g. finding `memory_descriptor_list` elsewhere confirms `MDL`
5. **Check `.ctx-cache/glossary.json`** (project glossary). If the abbreviation
   has a confirmed entry, use it and set `source_anchor` to `"glossary:user-confirmed"`.
Only if you find **direct evidence** in the code, docs, or glossary may you
include an explanation in the ctx JSON.

**F3. No evidence found = mark as unknown, NEVER invent**:
- In the ctx JSON, add the abbreviation to `unknown_fields`:
  `"unknown_fields": ["abbrev: MDL (no evidence found in codebase)"]`
- In the ctx JSON purpose/notes text, write: `[NEEDS VERIFICATION: MDL]`
  — do NOT write what you think it means
- If you are tempted to write "MDL stands for Memory Descriptor List": **stop**.
  That is hallucination unless the code itself contains that explanation.

**F4. Known-domain knowledge is still hallucination if not in THIS codebase**:
Even if you (the LLM) "know" that `IRP` means I/O Request Packet in Windows
drivers, you MUST NOT write that in the ctx JSON unless the project's own code
or docs say so. Different projects reuse abbreviations for different things.

**F5. After ALL modules generated: batch-ask user for unknown abbreviations**:
See "Glossary Collection" below. This runs once after Stage 2, not per-module.

For **each module** in the skeleton:
1. Follow the 5-step reading protocol above
2. Generate a JSON object matching this **exact schema**:

```json
{
  "module_id": "<COPY EXACT id FROM skeleton -- do not change>",
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
   - If you cannot determine a field, write `"UNKNOWN"` -- do NOT guess or paraphrase
   - `purpose` must be >= 50 chars and answer "This module exists to ___"
   - `public_api` must list EXACT signatures from source, not just names
     Bad: `["run_engine"]`  Good: `["run_engine(ctx: engine_ctx_t*) -> int"]`
   - `disclosure_hint` is the **most important field** -- must be >= 30 chars,
     describe what breaks if you ignore it, not just what the module does
     Bad: `"Core engine module"`  Good: `"Call engine_init() before run_engine(), not thread-safe"`
   - `design_notes` must contain at least 1 concrete architectural decision or constraint
   - **Every claim must have a source anchor** -- cite `file:line` where you read it
   - **NEVER explain an abbreviation unless you found its definition in the code/docs.**
     Writing "X stands for Y" without a source anchor is hallucination.
     When in doubt, write `[NEEDS_VERIFICATION: X]` and list X in `unknown_fields`.
   - If you cannot find a source line for a claim, put it in `unknown_fields`
   - Always set `"verified": false` -- only humans can mark it `true`
   - Save each module JSON to `.ctx-cache/ctx/<module_id>.json`
   - **Quality check before saving**: re-read your JSON. If `purpose` could describe
     ANY module in the project, rewrite it to be specific to THIS module.

### Stage 3: Validate (use MCP tool `validate_coverage`)
1. Call `validate_coverage` with `project_dir` and `ctx_dir=".ctx-cache/ctx"`
2. **[MANDATORY] Check `needs_user_input` in the response**:
   - If `true`: **STOP immediately** and go to Stage 3.5 (do NOT proceed to Stage 4)
   - The `glossary_prompts` field lists every unknown abbreviation with source modules
3. If `missing_ids` is non-empty, go back to Stage 2 for those modules
4. If `stale_ids` is non-empty, regenerate those modules
5. If `_fatal_errors` is non-empty, STOP and show errors to user.

### Stage 3.5: Confirm Unknown Abbreviations (MANDATORY if `needs_user_input=true`)

**This stage is NON-OPTIONAL. You MUST complete it before Stage 4.**

1. **Batch-ask the user** using the `AskUserQuestion` tool with ALL abbreviations
   from `glossary_prompts`:
   ```
   Question: "I found these abbreviations in the codebase that I couldn't verify from code comments or docs. Do you know what they stand for?"
   
   Context (show for each):
   - MDL: ? (seen in modules: core_engine, pkt_handler)
     Hint: "...process MDL queue..."
   - RCV_BUF: ? (seen in: net_layer)
   
   (Answer 'unknown' for any you don't know.)
   ```
   **Do NOT ask one-by-one** — batch ALL unknowns into ONE `AskUserQuestion` call.

2. **Write answers** to `.ctx-cache/glossary.json`:
   ```json
   {
     "MDL": {"meaning": "Memory Descriptor List", "confirmed_by": "user"},
     "RCV_BUF": {"meaning": "[UNKNOWN]"}
   }
   ```
   (Write `"[UNKNOWN]"` for ones the user doesn't know — prevents re-asking.)

3. **Re-render wiki**: Call `assemble_docs` again. It now loads the glossary
   and replaces `[NEEDS VERIFICATION: MDL]` with the confirmed meaning
   (marked with `[GLOSSARY]` badge instead of `[UNVERIFIED]`).

4. Now proceed to Stage 4.

**Glossary file guidance**:
- `.ctx-cache/glossary.json` is **project knowledge** — consider committing it
  to the repo (unlike `.ctx-cache/skeleton.json` which is a build artifact).
- If the project already has `docs/terminology.md` or similar, parse it and
  pre-fill the glossary before Stage 2.

### Stage 4: Assemble (MANDATORY -- use MCP tool `assemble_docs`)

**NEVER use `write_file` to create wiki MD files. Only `assemble_docs` can produce them.**

1. Call `assemble_docs` with `project_dir`, `ctx_dir=".ctx-cache/ctx"`,
   `out_docs="./docs"`
2. This produces wiki-style output:
   - `docs/wiki/INDEX.md` -- single entry point (~50-100 lines)
   - `docs/wiki/domains/<domain>/<module>.wiki.md` -- cross-linked pages
3. **Verify**: Confirm `docs/wiki/INDEX.md` exists. If not, `assemble_docs` failed -- check errors.
4. Report the final coverage percentage


### Stage 5: Codebase Analysis -- Clustering, Boundaries, Standards (recommended)

After all ctx JSONs are generated, validated, and glossary confirmed,
run **multi-dimensional codebase analysis** to extract:
1. **Module clusters** — functionally similar modules (by dependencies, data structures, naming)
2. **Module boundaries** — where new code should be placed
3. **Coding standards** — naming conventions, error handling, I-type interface priority

**Use the `analyze_codebase` MCP tool** (do NOT manually analyze):

```
Call: analyze_codebase(
  project_dir="<project_root>",
  ctx_dir=".ctx-cache/ctx",
  skeleton_path=".ctx-cache/skeleton.json",
  out_docs="./docs"
)
```

This tool automatically:
1. **Clusters modules** by multi-dimensional similarity:
   - Dependency overlap (shared dependencies)
   - Data structure overlap (shared structs/classes)
   - Naming pattern similarity (common prefixes/suffixes)
   → Output: `docs/MODULE_BOUNDARIES.md` with cluster definitions

2. **Identifies I-type interfaces** (names starting with `I`):
   - Scans all public_api and key_data_structures
   - Finds which modules call each I-type interface
   → Output: Interface priority rules in `docs/CODING_STANDARDS.md`

3. **Extracts coding standards**:
   - Naming conventions (per domain)
   - Error handling patterns
   - Module clustering recommendations
   → Output: `docs/CODING_STANDARDS.md`

**After the tool returns**:
- Read `docs/MODULE_BOUNDARIES.md` — understand where new code should go
- Read `docs/CODING_STANDARDS.md` — follow these conventions
- Review `clusters` in the tool response — check if any module should be split/merged
- Review `recommendations` — address any suggested improvements

**If Coraline is available**, the tool uses Coraline data for more precise analysis.


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
| `analyze_codebase` | Multi-dimensional analysis: cluster modules, identify boundaries, extract coding standards (including I-type interface priority) | No* |

\* `analyze_codebase` is deterministic (no LLM needed). It uses programmatic analysis of ctx JSONs.

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
