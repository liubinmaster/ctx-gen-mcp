"""ctx_gen_mcp/server.py — MCP Server with 3 deterministic tools.

Tools:
  scan_skeleton   — deterministic repo scan (no LLM)
  validate_coverage — programatic coverage check + stale detection
  assemble_docs    — merge all module JSONs into progressive MD docs

Usage (stdio transport, for OpenCode MCP config):
  python -m ctx_gen_mcp.server
  # or: ctx-gen-server
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# ── Constants ────────────────────────────────────────────────────────────────
DEFAULT_CODECCIES = {
    ".py", ".pyx", ".pyi",
    ".js", ".ts", ".jsx", ".tsx",
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".inl",
    ".go", ".rs", ".java", ".kt", ".swift",
    ".rb", ".php", ".cs", ".vb",
    ".R", ".lua", ".pl", ".sh", ".bash",
}
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB

mcp = FastMCP(
    name="ctx-gen",
    instructions=(
        "Deterministic tools for code context description generation. "
        "Call scan_skeleton first, then generate per-module descriptions (done by the Agent), "
        "then validate_coverage, then assemble_docs."
    ),
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _detect_language(ext: str) -> str:
    return {
        ".py": "python", ".pyx": "python", ".pyi": "python",
        ".js": "javascript", ".ts": "typescript", ".jsx": "javascript", ".tsx": "typescript",
        ".c": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
        ".h": "c", ".hpp": "cpp", ".hh": "cpp", ".inl": "cpp",
        ".go": "go", ".rs": "rust", ".java": "java",
        ".rb": "ruby", ".php": "php", ".cs": "csharp",
    }.get(ext, "unknown")


def _find_entry(files: list[str]) -> str | None:
    priority = ["__init__.py", "main.py", "index.js", "index.ts", "app.py",
                "program.cs", "main.go", "main.rs", "Main.java"]
    for p in priority:
        if p in files:
            return p
    h_files = [f for f in files if f.endswith((".h", ".hpp", ".hh"))]
    if h_files:
        return h_files[0]
    py_files = [f for f in files if f.endswith(".py")]
    if py_files:
        return py_files[0]
    return files[0] if files else None


def _read_file(path: Path) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except Exception:
            continue
    return ""


def _count_lines(path: Path) -> int:
    try:
        return len(_read_file(path).splitlines())
    except Exception:
        return 0


def _walk_dir(root: Path, code_only: bool) -> list[Path]:
    results: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.name.startswith(".") or child.name in (
            "node_modules", "__pycache__", "venv", ".venv",
            "build", "dist", "target", ".git", ".hg", ".svn",
        ):
            continue
        if child.is_dir():
            results.extend(_walk_dir(child, code_only))
        elif child.is_file():
            if code_only and child.suffix not in DEFAULT_CODECCIES:
                continue
            if child.stat().st_size > MAX_FILE_SIZE:
                continue
            results.append(child)
    return results


def _make_output_dirs(project_root: str, out_dir: str) -> tuple[Path, Path, Path]:
    out = Path(out_dir)
    ctx_dir = out / "ctx"
    docs_dir = out / "docs"
    modules_dir = docs_dir / "modules"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    modules_dir.mkdir(parents=True, exist_ok=True)
    return ctx_dir, docs_dir, modules_dir


def _list_existing_contexts(ctx_dir: Path) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if not ctx_dir.exists():
        return result
    for f in ctx_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            result[data["module_id"]] = data
        except Exception:
            continue
    return result


def _compute_file_hash(path: Path) -> str:
    try:
        return hashlib.md5(_read_file(path).encode()).hexdigest()[:12]
    except Exception:
        return ""


# ── Tools ────────────────────────────────────────────────────────────────────

def _scan_repo(root: Path, depth: int = 2, code_only: bool = True) -> dict:
    """Core scanning logic (not an MCP tool). Returns skeleton dict."""
    if not root.is_dir():
        return {"error": f"Not a directory: {root}"}

    all_files = _walk_dir(root, code_only=code_only)
    exts = {f.suffix for f in all_files}
    languages = {_detect_language(f.suffix) for f in all_files}
    total_lines = sum(_count_lines(f) for f in all_files)

    modules: list[dict] = []
    max_depth = max((len(f.relative_to(root).parts) for f in all_files), default=0)

    # Flat: all code files are directly in the root (max_depth == 1)
    # Hierarchical: code files are in subdirectories (max_depth >= 2)
    if max_depth <= 1 or depth <= 0:
        modules.append({
            "id": root.name or "root",
            "path": ".",
            "files": [f.name for f in all_files],
            "file_count": len(all_files),
            "language": next(iter(languages), "unknown"),
            "total_lines": total_lines,
            "entry": _find_entry([f.name for f in all_files]),
            "content_hash": hashlib.md5("".join(
                _read_file(f) for f in all_files[:50]
            ).encode()).hexdigest()[:12],
        })
    else:
        seen: set[str] = set()
        for f in all_files:
            parts = f.relative_to(root).parts
            if len(parts) < 2:
                continue
            mod_name = parts[0]
            if mod_name in seen:
                continue
            seen.add(mod_name)
            mod_dir = root / mod_name
            mod_files = [pf.relative_to(root).as_posix() for pf in _walk_dir(mod_dir, code_only)]
            mod_paths = [root / pf for pf in mod_files]
            mlang = {_detect_language(Path(pf).suffix) for pf in mod_files}
            mlang.discard("unknown")
            mlines = sum(_count_lines(p) for p in mod_paths)
            hash_parts = [_read_file(p) for p in mod_paths[:50]]
            modules.append({
                "id": mod_name,
                "path": mod_name,
                "files": mod_files,
                "file_count": len(mod_files),
                "language": next(iter(mlang), "unknown"),
                "total_lines": mlines,
                "entry": _find_entry([Path(pf).name for pf in mod_files]),
                "content_hash": hashlib.md5("".join(hash_parts).encode()).hexdigest()[:12],
            })

    return {
        "root_path": str(root),
        "total_modules": len(modules),
        "total_files": len(all_files),
        "total_lines": total_lines,
        "languages": sorted(languages),
        "extensions": sorted(exts),
        "modules": modules,
    }


@mcp.tool()
def scan_skeleton(project_dir: str, depth: int = 2, code_only: bool = True) -> dict:
    """Scan a code repository and return a deterministic module skeleton.

    Args:
        project_dir: Absolute path to the project root directory.
        depth: Directory depth for auto-detecting modules (default 2).
        code_only: If True, only include code files (default True).

    Returns:
        A dict with: root_path, total_modules, total_files, total_lines, modules[].
    """
    root = Path(project_dir).resolve()
    return _scan_repo(root, depth=depth, code_only=code_only)


@mcp.tool()
def validate_coverage(project_dir: str, ctx_dir: str, check_stale: bool = True) -> dict:
    """Validate that every module has a generated context JSON, and detect stale ones.

    Args:
        project_dir: Path to the project root.
        ctx_dir: Path to the ctx/ output directory.
        check_stale: If True, detect modules whose source has changed (default True).

    Returns:
        Dict with: total_modules, generated, coverage_pct, missing_ids[],
        stale_ids[], unknown_fields_summary{}.
    """
    root = Path(project_dir).resolve()
    ctx = Path(ctx_dir)
    skeleton = _scan_repo(root, depth=2, code_only=True)
    modules = {m["id"]: m for m in skeleton.get("modules", [])}

    existing = _list_existing_contexts(ctx)
    generated = len(existing)
    total = skeleton.get("total_modules", 0)
    pct = round(generated / total * 100, 1) if total > 0 else 0.0

    missing = [mid for mid in modules if mid not in existing]
    stale: list[str] = []
    unknown_summary: dict[str, list[str]] = {}

    for mid, data in existing.items():
        # Check stale
        if check_stale and mid in modules:
            mod = modules[mid]
            if mod.get("content_hash") != data.get("source_hash", ""):
                stale.append(mid)
        # Check unknown fields
        u = data.get("unknown_fields", [])
        if u:
            unknown_summary[mid] = u

    return {
        "total_modules": total,
        "generated": generated,
        "coverage_pct": pct,
        "missing_ids": missing,
        "stale_ids": stale,
        "unknown_fields_summary": unknown_summary,
    }


@mcp.tool()
def assemble_docs(project_dir: str, ctx_dir: str, out_docs: str, project_name: str = "") -> dict:
    """Assemble all per-module JSON context files into progressive-disclosure MD docs.

    Args:
        project_dir: Path to the project root.
        ctx_dir: Path to the ctx/ directory with per-module JSONs.
        out_docs: Output directory for MD docs.
        project_name: Optional project name (default: inferred from project_dir).

    Returns:
        Dict with: main_doc, module_docs[], errors[].
    """
    root = Path(project_dir).resolve()
    ctx = Path(ctx_dir)
    docs = Path(out_docs)
    modules_dir = docs / "modules"
    ctx.mkdir(parents=True, exist_ok=True)
    modules_dir.mkdir(parents=True, exist_ok=True)

    name = project_name or root.name or "Project"
    existing = _list_existing_contexts(ctx)
    modules = sorted(existing.values(), key=lambda x: x.get("purpose", ""))

    errors: list[str] = []
    module_docs: list[str] = []

    # L0 + L1 + L3: main doc
    lines: list[str] = []
    lines.append(f"# {name} — Code Context Reference\n")
    lines.append("> Progressive-disclosure context for AI coding agents.")
    lines.append("> Read L0 first, then L1 to find the right module, then open the L2 link.\n")

    # L0: One-liner
    lines.append("## [L0] What This Project Does\n")
    lines.append(f"> {name} — context description not yet generated. "
                "Run the ctx-gen agent to populate this section.\n")

    # L1: Module map
    lines.append("## [L1] Module Map\n")
    lines.append("| Module | Language | Files | Lines | Entry | Purpose |")
    lines.append("|---------|----------|-------|-------|-------|---------|")
    for m in modules:
        mid = m.get("module_id", "unknown")
        lang = m.get("language", "?")
        purpose = (m.get("purpose") or "?")[:60]
        entry = m.get("entry", "-")
        # Count files from source
        nfiles = len(m.get("dependencies", []))  # placeholder
        nlines = 0
        lines.append(f"| [{mid}](./modules/{mid}.md) | {lang} | ? | ? | {entry} | {purpose} |")
    lines.append("")

    # L3: Key decisions
    lines.append("## [L3] Key Design Decisions\n")
    adrs = [m for m in modules if m.get("design_notes")]
    if adrs:
        for m in adrs:
            lines.append(f"### {m.get('module_id')}")
            lines.append(f"{m.get('design_notes', '')}\n")
    else:
        lines.append("*Run ctx-gen agent to populate architecture decisions.*\n")

    # L2 hint
    lines.append("---\n")
    lines.append("## Module Details (L2)")
    lines.append("")
    lines.append("Each module has its own L2 detail page:")
    for m in modules:
        mid = m.get("module_id", "unknown")
        lines.append(f"- [{mid}](./modules/{mid}.md)")
    lines.append("")

    main_doc = docs / "PROJECT_CONTEXT.md"
    main_doc.write_text("\n".join(lines), encoding="utf-8")

    # Per-module L2 docs
    for m in modules:
        mid = m.get("module_id", "unknown")
        ml = [f"# {mid} — L2 Detail\n"]
        ml.append(f"**Language**: {m.get('language', '?')}  ")
        ml.append(f"**Entry**: `{m.get('entry', '-')}`  ")
        ml.append(f"**Files**: {', '.join(m.get('files', [])[:10])}\n")
        ml.append(f"## Purpose\n{m.get('purpose', '?')}\n")
        ml.append(f"## Public API\n")
        for fn in m.get("public_api", []):
            ml.append(f"- `{fn}`")
        ml.append("")
        ml.append(f"## Dependencies\n")
        for d in m.get("dependencies", []):
            ml.append(f"- {d}")
        ml.append("")
        ml.append(f"## Key Data Structures\n")
        for ds in m.get("key_data_structures", []):
            ml.append(f"### {ds.get('name', '?')}")
            ml.append(ds.get("description", ""))
        ml.append("")
        ml.append(f"## Design Notes\n{m.get('design_notes', '*None recorded*')}\n")
        ml.append(f"## Disclosure Hint\n{m.get('disclosure_hint', '')}\n")

        mod_doc = modules_dir / f"{mid}.md"
        mod_doc.write_text("\n".join(ml), encoding="utf-8")
        module_docs.append(str(mod_doc))

    return {
        "main_doc": str(main_doc),
        "module_docs": module_docs,
        "modules_processed": len(modules),
        "errors": errors,
    }


def mcp_main():
    """Entry point for `ctx-gen-server` CLI."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    mcp_main()
