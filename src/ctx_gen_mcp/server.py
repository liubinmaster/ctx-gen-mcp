"""ctx_gen_mcp/server.py -- MCP Server with 4 deterministic tools.

Tools:
  scan_skeleton    -- deterministic repo scan with domain grouping, tags, deps (no LLM)
  lookup           -- find modules by tag/domain/keyword
  validate_coverage -- programmatic coverage check + stale detection
  assemble_docs     -- merge all module JSONs into wiki-style MD docs

Output (wiki format):
  docs/wiki/INDEX.md           -- single entry point (~50 lines)
  docs/wiki/domains/<domain>/*.wiki.md  -- cross-linked per-module wiki pages

Usage (stdio transport, for OpenCode MCP config):
  python -m ctx_gen_mcp.server
  # or: ctx-gen-server
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP



# -- Fail-Fast Infrastructure --------------------------------------------------
#
# All tools MUST pre-validate inputs and raise _CtxGenError on any
# unexpected condition.  Silent fallbacks and 'except: pass' are forbidden.
#
# Error convention:
#   - Pre-condition failure (bad input, missing files):
#       raise _CtxGenError(message)  -- FastMCP returns this as an MCP error
#   - Processing issue (corrupt JSON, suspicious data):
#       return {'_fatal_errors': [...], 'status': 'error', ...}
#       Agent MUST check '_fatal_errors' and stop if present.
#   - Warning (recoverable):
#       return {'warnings': [...], 'status': 'ok', ...}

class _CtxGenError(ValueError):
    """Raised when a tool cannot proceed.  FastMCP surfaces the message to the agent."""
    def __init__(self, summary: str, details: str = '', how_to_fix: str = ''):
        self.summary = summary
        self.details = details
        self.how_to_fix = how_to_fix
        msg = f"[ctx-gen ERROR] {summary}"
        if details:
            msg += f"\n  Details: {details}"
        if how_to_fix:
            msg += f"\n  How to fix: {how_to_fix}"
        super().__init__(msg)


def _require(condition: bool, summary: str, **kwargs):
    """raise _CtxGenError if condition is False."""
    if not condition:
        raise _CtxGenError(summary, **kwargs)


def _validate_ctx_json(path: Path) -> list:
    """Validate a single ctx JSON file.  Returns list of error strings (empty = valid)."""
    errors = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"Invalid JSON at line {e.lineno}: {e.msg}"]
    except Exception as e:
        return [f"Cannot read file: {e}"]

    if not isinstance(data, dict):
        return ["Top-level JSON must be an object/dict, not a list."]
    mid = data.get("module_id", "")
    if not mid or not isinstance(mid, str):
        errors.append("Missing or invalid 'module_id' field (must be non-empty string).")
    if "source_hash" not in data:
        errors.append("Missing 'source_hash' field -- this ctx JSON may be from an old version.")
    return errors


# -- Constants ----------------------------------------------------------------

DEFAULT_CODE_EXTS = {
    ".py", ".pyx", ".pyi",
    ".js", ".ts", ".jsx", ".tsx",
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".inl",
    ".go", ".rs", ".java", ".kt", ".swift",
    ".rb", ".php", ".cs", ".vb",
    ".R", ".lua", ".pl", ".sh", ".bash",
}
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB

# Tag keyword maps for automatic inference
_ARCH_KEYWORDS = {
    "kernel": ["kernel", "driver_", "minifilter", "irp", "wdm", "ntoskrnl",
               "driverentry", "deviceiocontrol", "zwcreate", "psset"],
    "user-mode": ["win32", "winapi", "user32", "kernel32", "getprocaddress",
                  "createmutex", "virtualalloc", "createthread"],
    "shared-lib": ["__declspec", "dllexport", "dllmain", "so_init", "shared"],
}
_TECH_KEYWORDS = {
    "driver": ["driver", "filter", "deviceobject", "driverobject", "fltregister"],
    "crypto": ["crypto", "cipher", "encrypt", "decrypt", "aes", "rsa", "sha256",
               "sqlcipher", "openssl", "bcrypt", "hmac"],
    "network": ["socket", "http", "tcp", "udp", "listen", "connect", "bind",
                "wsa", "winsock", "ssl", "tls", "curl"],
    "async": ["async", "await", "future", "promise", "callback", "iocp",
              "epoll", "select", "completionport"],
    "ipc": ["pipe", "namedpipe", "rpc", "lpc", "alc", "message", "shm",
            "sharedmem", "mailbox", "dbus"],
}
_BUILD_FILES = {"CMakeLists.txt", "Makefile", "makefile", "*.sln", "*.vcxproj",
                "build.gradle", "Cargo.toml", "package.json", "setup.py",
                "pyproject.toml", "go.mod"}

# Include/import patterns for shallow dependency detection
_INCLUDE_PATTERNS = [
    # C/C++
    re.compile(r'#\s*include\s+[<"]([^>"]+)[>"]', re.IGNORECASE),
    re.compile(r'#\s*import\s+[<"]([^>"]+)[>"]', re.IGNORECASE),
    # Python
    re.compile(r'^(?:from|import)\s+([\w.]+)', re.MULTILINE),
    # JS/TS
    re.compile(r'(?:import|require)\s*\(?[\'"]([^\'"]+)[\'"]', re.IGNORECASE),
    # Go
    re.compile(r'import\s+"([^"]+)"'),
    # Rust
    re.compile(r'use\s+([\w:]+)::'),
    # Java/C#
    re.compile(r'import\s+([\w.]+)', re.IGNORECASE),
]

mcp = FastMCP(
    name="ctx-gen",
    instructions=(
        "Deterministic tools for code context wiki generation. "
        "Call scan_skeleton first to get the module skeleton with domains, tags, "
        "and dependency graph. Use lookup to find relevant modules by keyword/tag. "
        "Agent generates per-module context JSONs, then call validate_coverage which "
        "AUTOMATICALLY assembles wiki MD files (INDEX.md + .wiki.md pages). "
        "You do NOT need to call assemble_docs separately -- it runs inside validate_coverage."
    ),
)


# -- Helpers ------------------------------------------------------------------

def _detect_language(ext: str) -> str:
    return {
        ".py": "python", ".pyx": "python", ".pyi": "python",
        ".js": "javascript", ".ts": "typescript", ".jsx": "javascript",
        ".tsx": "typescript",
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
    for enc in ("utf-8", "latin-1", "gbk"):
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
            "egg-info", ".eggs", ".tox",
        ):
            continue
        if child.is_dir():
            results.extend(_walk_dir(child, code_only))
        elif child.is_file():
            if code_only and child.suffix not in DEFAULT_CODE_EXTS:
                continue
            if child.stat().st_size > MAX_FILE_SIZE:
                continue
            results.append(child)
    return results


def _list_existing_contexts(ctx_dir: Path) -> dict[str, dict]:
    """Load all existing ctx JSON files from ctx_dir.

    FAIL-FAST: Raises _CtxGenError on the FIRST corrupt JSON file.
    Silent skipping of corrupt files is NOT allowed.
    """
    result: dict[str, dict] = {}
    if not ctx_dir.exists():
        return result
    for f in ctx_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            result[data["module_id"]] = data
        except json.JSONDecodeError as e:
            raise _CtxGenError(
                f"Corrupt ctx JSON file: {f.name}",
                details=f"JSON error at line {e.lineno}: {e.msg}",
                how_to_fix=f"Delete {f} and re-run Stage 2 for that module."
            ) from e
        except KeyError as e:
            raise _CtxGenError(
                f"Invalid ctx JSON file: {f.name}",
                details=f"Missing required field: {e}",
                how_to_fix=f"Delete {f} and re-run Stage 2 for that module."
            ) from e
        except Exception as e:
            raise _CtxGenError(
                f"Error reading ctx JSON file: {f.name}",
                details=str(e),
                how_to_fix=f"Delete {f} and re-run Stage 2 for that module."
            ) from e
    return result


def _compute_file_hash(path: Path) -> str:
    try:
        return hashlib.md5(_read_file(path).encode()).hexdigest()[:12]
    except Exception:
        return ""


# -- Tag Inference -----------------------------------------------------------

def _infer_tags(root: Path, module_path: str,
                files: list[Path]) -> list[str]:
    """Infer tags from file names, directory names, and shallow content scan."""
    tags: list[str] = set()
    mod_dir = root / module_path

    # Language tag
    lang_counter: Counter = Counter()
    for f in files:
        lang_counter[_detect_language(f.suffix)] += 1
    if lang_counter:
        top_lang = lang_counter.most_common(1)[0][0]
        if top_lang != "unknown":
            tags.add(top_lang)

    # Architecture tags (from filenames + shallow content)
    all_content_parts: list[str] = []
    for f in files[:20]:  # limit to 20 files for performance
        content = _read_file(f)
        # Use filename first
        fname = f.stem.lower()
        for tag, keywords in _ARCH_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in fname:
                    tags.add(tag)
                    break
        # Then content (first 500 chars)
        all_content_parts.append(content[:500])

    combined = " ".join(all_content_parts).lower()
    for tag, keywords in _ARCH_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in combined:
                tags.add(tag)
                break

    # Tech feature tags
    for f in files[:20]:
        fname = f.stem.lower()
        for tag, keywords in _TECH_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in fname:
                    tags.add(tag)
                    break
    for tag, keywords in _TECH_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in combined:
                tags.add(tag)
                break

    # Build target detection from build system files
    parent_build = _detect_build_targets(root, module_path)
    for bt in parent_build:
        tags.add(bt)

    return sorted(tags)


def _detect_build_targets(root: Path, module_path: str) -> list[str]:
    """Detect build target type from build system files in or near the module."""
    targets: list[str] = []
    mod_dir = root / module_path

    for pattern in _BUILD_FILES:
        from pathlib import PurePosixPath
        p = PurePosixPath(pattern)
        matches = list(mod_dir.glob(p.name)) + list(mod_dir.glob(pattern))
        for bf in matches[:3]:
            content = _read_file(bf).lower()
            if "add_library" in content and ("shared" in content or "SHARED" in content):
                targets.append("shared-lib")
            elif "add_library" in content:
                targets.append("static-lib")
            elif "add_executable" in content or "target_link_libraries" in content:
                targets.append("exe")
            elif ".sln" in str(bf) or ".vcxproj" in str(bf):
                targets.append("visual-studio")
            elif "cargo.toml" in str(bf).lower():
                targets.append("rust-lib")
            elif "go.mod" in str(bf).lower():
                targets.append("go-mod")

    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result if result else ["unknown-build"]


# -- Shallow Dependency Detection --------------------------------------------

def _detect_dependencies(root: Path, module_path: str,
                         all_modules: dict[str, str],
                         files: list[Path]) -> dict[str, str]:
    """Shallow dependency detection via import/include pattern matching.

    Returns dict mapping dependent module_id -> relationship type.
    """
    deps: dict[str, str] = {}
    mod_dir = root / module_path
    other_ids = set(all_modules.keys()) - {module_path}

    # Build a map: directory name -> module_id for matching
    dir_to_module: dict[str, str] = {}
    for mid in other_ids:
        dir_to_module[Path(mid).name.lower()] = mid
        # Also map parent segments
        for part in Path(mid).parts:
            dir_to_module[part.lower()] = mid

    include_set: set[str] = set()

    for f in files[:30]:
        content = _read_file(f)
        for pattern in _INCLUDE_PATTERNS:
            for match in pattern.finditer(content):
                ref = match.group(1).lower().strip()
                # Strip file extensions and path prefixes
                ref_stem = Path(ref).stem.lower()
                # Check against other module names
                if ref_stem in dir_to_module:
                    include_set.add(dir_to_module[ref_stem])
                # Also check if the full ref matches
                for mid in other_ids:
                    if mid.lower() in ref or ref in mid.lower():
                        include_set.add(mid)

    for mid in include_set:
        deps[mid] = "imports"

    return deps


# -- Domain Grouping ----------------------------------------------------------

def _assign_domains(modules: list[dict]) -> list[dict]:
    """Assign domains to modules based on directory hierarchy.

    Strategy: Use the immediate parent directory name as the domain.
    Modules directly in root get assigned to '_root'.
    If any domain has >10 modules, mark it for potential LLM subdivide
    (the Agent can handle this later).
    """
    domain_map: dict[str, list[dict]] = {}
    large_domains: list[str] = []

    for mod in modules:
        mod_id = mod["id"]
        parts = Path(mod_id).parts
        if len(parts) >= 2:
            # Use the first directory segment as domain
            domain = parts[0]
        else:
            domain = "_root"
        mod["domain"] = domain
        domain_map.setdefault(domain, []).append(mod)

    # Check for large domains (>10 modules)
    for domain, mods in domain_map.items():
        if len(mods) > 10:
            large_domains.append(domain)

    # Sort domains alphabetically
    for mod in modules:
        pass  # domain already assigned

    return modules, large_domains


# -- Core Scan ----------------------------------------------------------------

def _scan_repo(root: Path, depth: int = 2,
               code_only: bool = True) -> dict:
    """Core scanning logic. Returns skeleton with domains, tags, dependencies."""
    if not root.is_dir():
        return {"error": f"Not a directory: {root}"}

    all_files = _walk_dir(root, code_only=code_only)
    exts = {f.suffix for f in all_files}
    languages = {_detect_language(f.suffix) for f in all_files if _detect_language(f.suffix) != "unknown"}
    total_lines = sum(_count_lines(f) for f in all_files)

    modules: list[dict] = []
    max_depth = max((len(f.relative_to(root).parts) for f in all_files), default=0)

    if max_depth <= 1 or depth <= 0:
        # Flat project: all files in root
        all_content = "".join(_read_file(f) for f in all_files[:50])
        modules.append({
            "id": root.name or "root",
            "path": ".",
            "files": [f.name for f in all_files],
            "file_count": len(all_files),
            "language": next(iter(languages), "unknown"),
            "total_lines": total_lines,
            "entry": _find_entry([f.name for f in all_files]),
            "content_hash": hashlib.md5(all_content.encode()).hexdigest()[:12],
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

    # Module id -> path map for dependency detection
    module_map = {m["id"]: m["path"] for m in modules}

    # Enrich each module with tags and dependencies
    for mod in modules:
        mod_files = [root / f for f in mod["files"]]
        mod["tags"] = _infer_tags(root, mod["path"], mod_files)
        mod["dependencies"] = _detect_dependencies(root, mod["path"], module_map, mod_files)

    # Assign domains
    modules, large_domains = _assign_domains(modules)

    # Build domain summary
    domain_summary: dict[str, dict] = {}
    for mod in modules:
        d = mod["domain"]
        if d not in domain_summary:
            domain_summary[d] = {
                "module_count": 0,
                "total_lines": 0,
                "languages": [],
                "tags": [],
            }
        domain_summary[d]["module_count"] += 1
        domain_summary[d]["total_lines"] += mod["total_lines"]
        for t in mod.get("tags", []):
            if t not in domain_summary[d]["tags"]:
                domain_summary[d]["tags"].append(t)
        lang = mod.get("language", "unknown")
        if lang not in domain_summary[d]["languages"]:
            domain_summary[d]["languages"].append(lang)

    return {
        "root_path": str(root),
        "total_modules": len(modules),
        "total_files": len(all_files),
        "total_lines": total_lines,
        "languages": sorted(languages),
        "extensions": sorted(exts),
        "modules": modules,
        "domains": domain_summary,
        "large_domains": large_domains,
    }


# -- MCP Tools ----------------------------------------------------------------

@mcp.tool()
def scan_skeleton(project_dir: str, depth: int = 2,
                  code_only: bool = True) -> dict:
    """Scan a code repository and return a deterministic module skeleton
    with domain grouping, tags, and dependency graph.

    Args:
        project_dir: Absolute path to the project root directory.
        depth: Directory depth for auto-detecting modules (default 2).
        code_only: If True, only include code files (default True).

    Returns:
        A dict with: root_path, total_modules, total_files, total_lines,
        languages, extensions, modules[] (each with domain, tags,
        dependencies), domains{} (summary per domain),
        large_domains[] (domains with >10 modules needing subdivision).
    """
    _require(project_dir and isinstance(project_dir, str),
             "project_dir must be a non-empty string.",
             how_to_fix="Pass the absolute path to the project root directory.")
    root = Path(project_dir).resolve()
    _require(root.exists(),
             f"project_dir does not exist: {root}",
             how_to_fix=f"Check the path.  Current working directory is {{Path.cwd()}}.")
    _require(root.is_dir(),
             f"project_dir exists but is not a directory: {root}",
             how_to_fix="Pass a directory path, not a file path.")
    try:
        return _scan_repo(root, depth=depth, code_only=code_only)
    except Exception as e:
        raise _CtxGenError(
            f"_scan_repo failed unexpectedly: {e}",
            details=f"project_dir={root}, depth={depth}",
            how_to_fix="Check that the project directory contains readable code files.  "
                      "This may also be a bug -- please report it."
        ) from e


@mcp.tool()
def lookup(skeleton_json: str, query: str,
            lookup_type: str = "auto", ctx_dir: str = "") -> dict:
    """Find relevant modules by tag, domain, or keyword.

    Returns matched module IDs **with a purpose summary for each**,
    so the agent can judge which module is the correct target.

    Args:
        skeleton_json: The skeleton JSON string (from scan_skeleton output).
        query: Search query -- a tag name, domain name, keyword, or module id.
        lookup_type: One of "auto", "tag", "domain", "keyword", "id".
            "auto" tries all strategies and returns the best match.
        ctx_dir: Optional path to .ctx-cache/ctx/ directory.
                  If provided (and ctx JSONs exist), each candidate includes
                  its "purpose" field so the agent can disambiguate.

    Returns:
        Dict with: matched_ids[], match_reason, matched_domains[],
                 candidates[{id, domain, tags, purpose}].
    """
    # Pre-condition checks (fail-fast)
    _require(skeleton_json and isinstance(skeleton_json, str),
             "skeleton_json must be a non-empty JSON string.",
             how_to_fix="Call scan_skeleton first, then pass its output JSON string.")
    _require(query and isinstance(query, str),
             "query must be a non-empty string.",
             how_to_fix="Provide a search query (module id, tag, domain, or keyword).")
    _require(lookup_type in ("auto", "tag", "domain", "keyword", "id"),
             f"lookup_type must be one of: auto, tag, domain, keyword, id. Got: {lookup_type}",
             how_to_fix="Use 'auto' if unsure.")

    # Parse skeleton JSON (raise on failure -- no silent fallback)
    try:
        skeleton = json.loads(skeleton_json)
    except json.JSONDecodeError as e:
        raise _CtxGenError(
            f"Invalid skeleton JSON: {e.msg}",
            details=f"Line {e.lineno}, column {e.colno}.  "
                      f"Skeleton JSON starts with: {skeleton_json[:100]!r}...",
            how_to_fix="The skeleton_json argument appears to be corrupted.  "
                      "Re-call scan_skeleton to get a fresh skeleton JSON."
        ) from e

    _require(isinstance(skeleton, dict),
             f"skeleton_json must parse to a dict, got {type(skeleton).__name__}.",
             how_to_fix="Pass the raw JSON string from scan_skeleton output, not a processed value.")

    modules = skeleton.get("modules", [])
    domains = skeleton.get("domains", {})
    q_lower = query.strip().lower()

    # Pre-load purpose summaries from ctx JSONs (if available)
    # Raise on corrupt ctx JSONs (don't silently skip)
    purpose_map: dict[str, str] = {}
    if ctx_dir:
        ctx_path = Path(ctx_dir)
        _require(ctx_path.exists(),
                 f"ctx_dir does not exist: {ctx_path}",
                 how_to_fix="Check that Stage 2 (Generate) has completed and ctx JSONs were saved.")
        _require(ctx_path.is_dir(),
                 f"ctx_dir exists but is not a directory: {ctx_path}")
        for jf in ctx_path.glob("*.json"):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
            except Exception as e:
                raise _CtxGenError(
                    f"Corrupt ctx JSON file: {jf.name}",
                    details=str(e),
                    how_to_fix=f"Delete {jf} and re-run Stage 2 for that module."
                ) from e
            mid = data.get("module_id", "")
            purpose_map[mid] = data.get("purpose", "")[:120]

    if not q_lower:
        # Return all modules with their purpose as candidates
        candidates = []
        for m in modules:
            mid = m["id"]
            candidates.append({
                "id": mid,
                "domain": m.get("domain", ""),
                "tags": m.get("tags", []),
                "purpose": purpose_map.get(mid, ""),
            })
        return {"matched_ids": [], "match_reason": "empty query",
                "matched_domains": [], "candidates": candidates}

    matched_ids: list[str] = []
    matched_domains: list[str] = []
    reason: str = ""

    if lookup_type in ("auto", "id"):
        # Exact module id match
        for m in modules:
            if m["id"].lower() == q_lower:
                matched_ids.append(m["id"])
                reason = f"exact module id match: {m['id']}"
                # Build candidates list for the single matched module
                mid = m["id"]
                candidates = [{
                    "id": mid,
                    "domain": m.get("domain", ""),
                    "tags": m.get("tags", []),
                    "purpose": purpose_map.get(mid, ""),
                }]
                return {"matched_ids": matched_ids, "match_reason": reason,
                        "matched_domains": [m.get("domain", "")],
                        "candidates": candidates}

    if lookup_type in ("auto", "domain"):
        # Domain match
        for d_name in domains:
            if q_lower in d_name.lower():
                matched_domains.append(d_name)
                for m in modules:
                    if m.get("domain") == d_name:
                        matched_ids.append(m["id"])
                reason = f"domain match: {d_name}"
                if matched_ids:
                    candidates = []
                    for mid in matched_ids:
                        m = next((x for x in modules if x["id"] == mid), {})
                        candidates.append({
                            "id": mid,
                            "domain": m.get("domain", ""),
                            "tags": m.get("tags", []),
                            "purpose": purpose_map.get(mid, ""),
                        })
                    return {"matched_ids": matched_ids, "match_reason": reason,
                            "matched_domains": matched_domains,
                            "candidates": candidates}

    if lookup_type in ("auto", "tag"):
        # Tag match
        for m in modules:
            tags = m.get("tags", [])
            for t in tags:
                if q_lower in t.lower():
                    if m["id"] not in matched_ids:
                        matched_ids.append(m["id"])
                    reason = f"tag match: {query}"
                    break

    if lookup_type in ("auto", "keyword"):
        # Keyword match against module id and files
        if not matched_ids:
            for m in modules:
                mid = m["id"].lower()
                files_str = " ".join(m.get("files", [])).lower()
                if q_lower in mid or q_lower in files_str:
                    if m["id"] not in matched_ids:
                        matched_ids.append(m["id"])
                reason = f"keyword match: {query}"

    if not matched_ids and not reason:
        reason = "no matches found"

    # Build candidates list for all matched_ids
    candidates = []
    for mid in matched_ids:
        m = next((x for x in modules if x["id"] == mid), {})
        candidates.append({
            "id": mid,
            "domain": m.get("domain", ""),
            "tags": m.get("tags", []),
            "purpose": purpose_map.get(mid, ""),
        })

    return {
        "matched_ids": matched_ids,
        "match_reason": reason or "no matches found",
        "matched_domains": matched_domains,
        "candidates": candidates,
    }


@mcp.tool()
@mcp.tool()
def validate_coverage(project_dir: str, ctx_dir: str,
                     check_stale: bool = True) -> dict:
    """Validate that every module has a generated context JSON, and detect stale ones.

    **IMPORTANT**: This tool automatically calls  if any context
    JSONs exist, so wiki MD files are always generated. No need to call
    assemble_docs separately.

    Args:
        project_dir: Path to the project root.
        ctx_dir: Path to the ctx/ output directory.
        check_stale: If True, detect modules whose source has changed (default True).

    Returns:
        Dict with: total_modules, generated, coverage_pct, missing_ids[],
        stale_ids[], unknown_fields_summary{}, wiki_auto_generated,
        wiki_index (path), wiki_modules (count),
        glossay_count, glossay_errors[],
        glossay_prompts[{abbrev, modules[], context_hint}],
        needs_user_input (bool -- True if there are unknown abbreviations),
        hallucination_warnings[], anchor_errors[],
        _fatal_errors[] (non-empty = output is unreliable, agent MUST stop),
        status ('ok' or 'error').
        ***IMPORTANT***: If `needs_user_input` is True, the agent MUST
        STOP and ask the user to confirm the abbreviations in `glossary_prompts`
        BEFORE proceeding to any further steps.  This is not optional.
    """
    # Pre-condition checks (fail-fast)
    _require(project_dir and isinstance(project_dir, str),
             "project_dir must be a non-empty string.")
    _require(ctx_dir and isinstance(ctx_dir, str),
             "ctx_dir must be a non-empty string.",
             how_to_fix="Pass the path to .ctx-cache/ctx/ directory.")

    root = Path(project_dir).resolve()
    ctx = Path(ctx_dir)
    _require(root.exists(), f"project_dir does not exist: {root}")
    _require(root.is_dir(), f"project_dir exists but is not a directory: {root}")

    # Scan repo (raise on failure -- no silent fallback)
    try:
        skeleton = _scan_repo(root, depth=2, code_only=True)
    except Exception as e:
        raise _CtxGenError(
            f"_scan_repo failed: {e}",
            details=f"project_dir={root}",
            how_to_fix="Check that the project directory is accessible and contains code files."
        ) from e

    modules = {m["id"]: m for m in skeleton.get("modules", [])}

    # Validate all existing ctx JSON files (fail on corrupt files)
    existing = _list_existing_contexts(ctx)
    fatal_errors: list[str] = []
    for mid in existing:
        ctx_path = ctx / f"{mid}.json"
        if ctx_path.exists():
            errs = _validate_ctx_json(ctx_path)
            if errs:
                for err in errs:
                    fatal_errors.append(f"[{mid}.json] {err}")

    generated = len(existing)
    total = skeleton.get("total_modules", 0)
    pct = round(generated / total * 100, 1) if total > 0 else 0.0

    # Load project glossary for stats (report errors, don't silently pass)
    glossary_count = 0
    glossary_errors: list[str] = []
    glossary_path = root / ".ctx-cache" / "glossary.json"
    if glossary_path.exists():
        try:
            raw = json.loads(glossary_path.read_text(encoding="utf-8"))
            _require(isinstance(raw, dict),
                     f"glossary.json must be a JSON dict, got {type(raw).__name__}.",
                     how_to_fix=f"Delete {glossary_path} and re-run Stage 2.5 (Glossary Collection).")
            for k, v in raw.items():
                meaningful = False
                if isinstance(v, str) and v != "[UNKNOWN]":
                    meaningful = True
                elif isinstance(v, dict) and v.get("meaning", "") != "[UNKNOWN]":
                    meaningful = True
                if meaningful:
                    glossary_count += 1
        except json.JSONDecodeError as e:
            glossary_errors.append(f"glossary.json is corrupt JSON: {e.msg} (line {e.lineno})")
        except _CtxGenError as e:
            glossary_errors.append(e.summary)
        except Exception as e:
            glossary_errors.append(f"Unexpected error reading glossary.json: {e}")

    missing = [mid for mid in modules if mid not in existing]
    stale: list[str] = []
    unknown_summary: dict[str, list[str]] = {}
    # Also collect glossary prompts (unknow abbreviations) across all modules
    glossary_prompts: list[dict] = []
    _seen_abbrevs: dict[str, list[str]] = {}  # abbrev -> list of module ids

    for mid, data in existing.items():
        if check_stale and mid in modules:
            mod = modules[mid]
            if mod.get("content_hash") != data.get("source_hash", ""):
                stale.append(mid)
        u = data.get("unknown_fields", [])
        if u:
            unknown_summary[mid] = u
            # Extract abbreviation entries from unknown_fields
            for entry in u:
                # Match "abbrev: MDL (no evidence...)" or "[NEEDS VERIFICATION: MDL]"
                import re
                # Pattern 1: "abbrev: ABBREV ..."
                m = re.search(r'abbrev:\s*(\S+)', entry)
                if m:
                    abbr = m.group(1).strip(')').upper()
                    if abbr not in _seen_abbrevs:
                        _seen_abbrevs[abbr] = []
                    _seen_abbrevs[abbr].append(mid)
                # Pattern 2: "[NEEDS VERIFICATION: ABBREV]"
                for m2 in re.finditer(r'\[NEEDS VERIFICATION:\s*(\S+?)\s*\]', entry, re.IGNORECASE):
                    abbr = m2.group(1).strip(')').upper()
                    if abbr not in _seen_abbrevs:
                        _seen_abbrevs[abbr] = []
                    _seen_abbrevs[abbr].append(mid)
        # Also scan text fields for [NEEDS VERIFICATION: X] tokens
        for field in ("purpose", "design_notes", "disclosure_hint"):
            text = data.get(field, "")
            if not text or not isinstance(text, str):
                continue
            for m in re.finditer(r'\[NEEDS VERIFICATION:\s*(\S+?)\s*\]', text, re.IGNORECASE):
                abbr = m.group(1).strip(')').upper()
                if abbr not in _seen_abbrevs:
                    _seen_abbrevs[abbr] = []
                if mid not in _seen_abbrevs[abbr]:
                    _seen_abbrevs[abbr].append(mid)

    # Filter out already-confirmed abbreviations from glossary.json
    confirmed: set[str] = set()
    if glossary_path.exists():
        try:
            raw = json.loads(glossary_path.read_text(encoding="utf-8"))
            for k, v in raw.items():
                meaning = v if isinstance(v, str) else v.get("meaning", "") if isinstance(v, dict) else ""
                if meaning and meaning != "[UNKNOWN]":
                    confirmed.add(k.upper())
        except Exception:
            pass  # glossary_errors already captured above
    for abbr, mods in _seen_abbrevs.items():
        if abbr in confirmed:
            continue
        # Get context: find the line containing this abbrev in the module's ctx
        context_hint = ""
        for mid in mods:
            d = existing.get(mid, {})
            pur = d.get("purpose", "")
            if abbr in pur.upper():
                # Extract a short context snippet
                idx = pur.upper().find(abbr)
                start = max(0, idx - 40)
                end = min(len(pur), idx + len(abbr) + 40)
                context_hint = pur[start:end].replace("\n", " ")
                break
        glossary_prompts.append({
            "abbrev": abbr,
            "modules": mods,
            "context_hint": context_hint,
        })

    # Auto-assemble wiki docs if any context JSONs exist
    wiki_result = None
    out_docs_default = str(root / "docs")
    hallucination_warnings: list[str] = []
    anchor_errors: list[str] = []

    if generated > 0:
        wiki_result = assemble_docs(str(root), str(ctx), out_docs_default)
        if wiki_result.get("errors"):
            unknown_summary["_wiki_errors"] = wiki_result["errors"]
        for mid, data in existing.items():
            hw = _detect_hallucinations(data)
            hallucination_warnings.extend(hw)
            ae = _validate_source_anchors(data, root)
            anchor_errors.extend(ae)

    result = {
        "total_modules": total,
        "generated": generated,
        "coverage_pct": pct,
        "missing_ids": missing,
        "stale_ids": stale,
        "unknown_fields_summary": unknown_summary,
        "glossary_count": glossary_count,
        "glossary_errors": glossary_errors,
        "glossary_prompts": glossary_prompts,
        "needs_user_input": len(glossary_prompts) > 0,
        "next_action": "ask_user_glossary" if glossary_prompts else ("proceed_to_assemble" if generated > 0 else "generate_context"),
        "hallucination_warnings": hallucination_warnings,
        "anchor_errors": anchor_errors,
        "wiki_auto_generated": wiki_result is not None,
        "wiki_index": wiki_result.get("index_doc", "") if wiki_result else "",
        "wiki_modules": wiki_result.get("modules_processed", 0) if wiki_result else 0,
    }
    if fatal_errors:
        result["_fatal_errors"] = fatal_errors
        result["status"] = "error"
    else:
        result["status"] = "ok"
    return result


@mcp.tool()
def assemble_docs(project_dir: str, ctx_dir: str,
                  out_docs: str, project_name: str = "") -> dict:
    """Assemble all per-module JSON context files into wiki-style MD docs
    with cross-links, domain grouping, and navigable INDEX.

    Args:
        project_dir: Path to the project root.
        ctx_dir: Path to the ctx/ directory with per-module JSONs.
        out_docs: Output directory for MD docs.
        project_name: Optional project name (default: inferred from project_dir).

    Returns:
        Dict with: index_doc, module_docs[], domain_dirs[], errors[],
                 _fatal_errors[] (non-empty = wiki is unreliable).
    """
    # Pre-condition checks (fail-fast)
    _require(project_dir and isinstance(project_dir, str),
             "project_dir must be a non-empty string.")
    _require(ctx_dir and isinstance(ctx_dir, str),
             "ctx_dir must be a non-empty string.")

    root = Path(project_dir).resolve()
    ctx = Path(ctx_dir)
    _require(root.exists(), f"project_dir does not exist: {root}")
    _require(root.is_dir(), f"project_dir exists but is not a directory: {root}")

    docs = Path(out_docs)
    wiki_dir = docs / "wiki"
    domains_dir = wiki_dir / "domains"
    ctx.mkdir(parents=True, exist_ok=True)
    domains_dir.mkdir(parents=True, exist_ok=True)

    name = project_name or root.name or "Project"
    existing = _list_existing_contexts(ctx)

    # Load project glossary (fail on corrupt file -- don't silently pass)
    glossary: dict = {}
    glossary_errors: list[str] = []
    glossary_path = root / ".ctx-cache" / "glossary.json"
    if glossary_path.exists():
        try:
            raw = json.loads(glossary_path.read_text(encoding="utf-8"))
            _require(isinstance(raw, dict),
                     f"glossary.json must be a JSON dict, got {type(raw).__name__}.",
                     how_to_fix=f"Delete {glossary_path} and re-run Stage 2.5.")
            for k, v in raw.items():
                if isinstance(v, str) and v != "[UNKNOWN]":
                    glossary[k.upper()] = v
                elif isinstance(v, dict) and v.get("meaning", "") != "[UNKNOWN]":
                    glossary[k.upper()] = v["meaning"]
        except json.JSONDecodeError as e:
            glossary_errors.append(f"glossary.json corrupt JSON: {e.msg} (line {e.lineno})")
        except _CtxGenError as e:
            glossary_errors.append(e.summary)
        except Exception as e:
            glossary_errors.append(f"Unexpected error reading glossary.json: {e}")

    def _apply_glossary(text: str) -> tuple[str, list[str]]:
        """Replace `[NEEDS VERIFICATION: X]` tokens with glossary entries.

        Returns (updated_text, list_of_glossary_hits).
        """
        if not text or not glossary:
            return text, []
        hits: list[str] = []
        for abbrev, meaning in glossary.items():
            token = f"[NEEDS VERIFICATION: {abbrev}]"
            if token in text and meaning != "[UNKNOWN]":
                text = text.replace(token, f"`{abbrev}` = {meaning}  [GLOSSARY]")
                hits.append(abbrev)
        return text, hits

    # Re-scan to get domain/tag info
    skeleton = _scan_repo(root, depth=2, code_only=True)
    all_modules = {m["id"]: m for m in skeleton.get("modules", [])}
    domain_summary = skeleton.get("domains", {})

    # Build a fallback lookup: file stems and path parts -> ctx_data
    # This handles the common case where agent wrote module_id by filename
    # (e.g. "engine") but skeleton id is a directory (e.g. "src")
    def _find_ctx_data(mid: str, mod_files: list[str]) -> dict:
        """Find ctx data by exact id match first, then by file/path heuristics."""
        if mid in existing:
            return existing[mid]
        # Try matching by files: if any ctx JSON covers the same file stems
        file_stems = {Path(f).stem.lower() for f in mod_files}
        for ctx_id, ctx_data in existing.items():
            # ctx JSON module_id matches a file stem in this module
            if ctx_id.lower() in file_stems:
                return ctx_data
            # ctx JSON covers files that overlap with this module
            ctx_files = ctx_data.get("files", [])
            if ctx_files:
                ctx_stems = {Path(f).stem.lower() for f in ctx_files}
                if file_stems & ctx_stems:
                    return ctx_data
        return {}

    errors: list[str] = []
    module_docs: list[str] = []
    domain_dirs: list[str] = []

    # --- Generate per-module wiki pages ---
    for mod in skeleton.get("modules", []):
        mid = mod["id"]
        domain = mod.get("domain", "_root")
        ctx_data = _find_ctx_data(mid, mod.get("files", []))
        tags = mod.get("tags", [])
        deps = mod.get("dependencies", {})
        used_by = {m["id"]: "imports" for m in skeleton.get("modules", [])
                   if mid in m.get("dependencies", {})}

        # Create domain subdirectory
        dom_dir = domains_dir / domain
        dom_dir.mkdir(parents=True, exist_ok=True)
        if domain not in domain_dirs:
            domain_dirs.append(domain)

        # Build wiki page
        lines: list[str] = []

        # YAML front matter
        lines.append("---")
        lines.append(f"id: {mid}")
        lines.append(f"domain: {domain}")
        lines.append(f"tags: [{', '.join(tags)}]")
        lines.append(f"depends_on: [{', '.join(deps.keys())}]")
        lines.append(f"used_by: [{', '.join(used_by.keys())}]")
        lines.append(f"language: {mod.get('language', 'unknown')}")
        lines.append(f"files: {mod.get('file_count', 0)}")
        lines.append(f"lines: {mod.get('total_lines', 0)}")
        lines.append(f"entry: {mod.get('entry', '-')}")
        # Verified status: deterministic fields are always true,
        # LLM-generated fields start as false until human review
        ctx_verified = ctx_data.get("verified", False)
        lines.append(f"verified: {'true' if ctx_verified else 'false'}")
        lines.append("---")
        lines.append("")

        # L0: One-liner (AI decides in 5 seconds if relevant)
        purpose = ctx_data.get("purpose", "(context not yet generated)")
        lines.append(f"# {mid} -- {purpose}")
        lines.append("")

        # L1: Summary (AI decides whether to read deeper)
        lines.append("## Summary")
        lines.append(f"**Domain**: `{domain}` | "
                     f"**Language**: {mod.get('language', '?')} | "
                     f"**Size**: {mod.get('file_count', 0)} files, "
                     f"{mod.get('total_lines', 0)} lines")
        if tags:
            lines.append(f"**Tags**: {', '.join(f'`{t}`' for t in tags)}")
        lines.append("")

        # Cross-link navigation
        nav_parts: list[str] = []
        if deps:
            dep_links = [f"[{d}](../{dep_domain(d, skeleton)}/{d}.wiki.md)"
                        for d in deps]
            nav_parts.append("Depends: " + ", ".join(dep_links))
        if used_by:
            ub_links = [f"[{u}](../{dep_domain(u, skeleton)}/{u}.wiki.md)"
                        for u in used_by]
            nav_parts.append("Used by: " + ", ".join(ub_links))
        if nav_parts:
            lines.append(" | ".join(nav_parts))
            lines.append("")

        # L2: Detailed content (read on demand)
        # Fields generated by LLM are marked with reliability badges:
        #   [VERIFIED]   -- human has reviewed and confirmed
        #   [UNVERIFIED] -- LLM-generated, not yet reviewed
        #   [UNKNOWN]    -- LLM could not determine
        # Source anchors (line numbers) let AI trace back to verify claims.
        # If a field has substantial text but NO source_anchor, append
        # [⚠️ NO SOURCE] warning to alert readers of hallucination risk.

        badge = "VERIFIED" if ctx_verified else "UNVERIFIED"
        has_ctx = bool(ctx_data.get("purpose"))
        anchors = ctx_data.get("source_anchors", {})

        lines.append("---")
        # Purpose
        purpose_text = ctx_data.get("purpose", "")
        purpose_text, glos_hits = _apply_glossary(purpose_text)
        purpose_anchors = anchors.get("purpose", [])
        purpose_warn = ""
        if purpose_text and purpose_text not in ("UNKNOWN", "?") and not purpose_anchors and not glos_hits:
            purpose_warn = " ⚠️ NO SOURCE ANCHOR"
        badge_g = badge if not glos_hits else "GLOSSARY"
        lines.append(f"## Purpose [{badge_g}]{purpose_warn}")
        if not purpose_text or purpose_text in ("UNKNOWN", "?"):
            purpose_text = "*Run ctx-gen agent to generate context for this module.*"
        lines.append(purpose_text)
        if glos_hits:
            lines.append(f"<!-- Glossary resolved: {', '.join(glos_hits)} -->")
        if purpose_anchors:
            lines.append(f"<!-- source: {', '.join(purpose_anchors)} -->")
        lines.append("")

        # Public API
        lines.append(f"## Public API [{badge}]")
        pub_api = ctx_data.get("public_api", [])
        if pub_api:
            api_anchors = anchors.get("public_api", {})
            for fn in pub_api:
                fn_base = fn.rstrip("()")
                anchor = api_anchors.get(fn, "") or api_anchors.get(fn_base, "")
                if anchor:
                    lines.append(f"- `{fn}` <!-- source: {anchor} -->")
                else:
                    lines.append(f"- `{fn}`")
        else:
            lines.append("*Not yet generated.*")
        lines.append("")

        # Key Data Structures
        lines.append(f"## Key Data Structures [{badge}]")
        kds = ctx_data.get("key_data_structures", [])
        if kds:
            kds_anchors = anchors.get("key_data_structures", {})
            for ds in kds:
                name = ds.get("name", "?")
                desc = ds.get("description", "")
                desc, _ = _apply_glossary(desc)
                lines.append(f"### {name}")
                lines.append(desc)
                anchor = kds_anchors.get(name, "")
                if anchor:
                    lines.append(f"<!-- source: {anchor} -->")
                elif desc and len(desc) > 20 and not _[1]:
                    lines.append("<!-- ⚠️ NO SOURCE ANCHOR for this description -->")
        else:
            lines.append("*Not yet generated.*")
        lines.append("")

        # Design Notes
        notes = ctx_data.get("design_notes", "")
        notes, notes_glos_hits = _apply_glossary(notes)
        notes_anchors = anchors.get("design_notes", [])
        notes_warn = ""
        if notes and notes not in ("UNKNOWN", "?", "") and not notes_anchors and not notes_glos_hits:
            notes_warn = " ⚠️ NO SOURCE ANCHOR"
        badge_g2 = badge if not notes_glos_hits else "GLOSSARY"
        lines.append(f"## Design Notes [{badge_g2}]{notes_warn}")
        if not notes or notes in ("UNKNOWN", "?", ""):
            notes = "*Not yet generated.*"
        lines.append(notes)
        if notes_glos_hits:
            lines.append(f"<!-- Glossary resolved: {', '.join(notes_glos_hits)} -->")
        if notes_anchors:
            lines.append(f"<!-- source: {', '.join(notes_anchors)} -->")
        lines.append("")

        # Disclosure Hint
        hint = ctx_data.get("disclosure_hint", "")
        hint, hint_glos_hits = _apply_glossary(hint)
        hint_warn = ""
        if hint and hint not in ("UNKNOWN", "?", "") and not anchors.get("disclosure_hint", []) and not hint_glos_hits:
            hint_warn = " ⚠️ NO SOURCE ANCHOR"
        badge_g3 = badge if not hint_glos_hits else "GLOSSARY"
        lines.append(f"## Disclosure Hint [{badge_g3}]{hint_warn}")
        if not hint or hint in ("UNKNOWN", "?", ""):
            hint = "*Not yet generated.*"
        lines.append(f"> {hint}")
        if hint_glos_hits:
            lines.append(f"<!-- Glossary resolved: {', '.join(hint_glos_hits)} -->")
        lines.append("")

        wiki_path = dom_dir / f"{mid}.wiki.md"
        wiki_path.write_text("\n".join(lines), encoding="utf-8")
        module_docs.append(str(wiki_path))

    # --- Generate INDEX.md ---
    idx_lines: list[str] = []
    idx_lines.append(f"# {name} -- Code Wiki Index")
    idx_lines.append(f"> Total: {skeleton.get('total_modules', 0)} modules | "
                     f"{skeleton.get('total_lines', 0)} lines | "
                     f"Languages: {', '.join(skeleton.get('languages', []))}")
    idx_lines.append("")

    # L0: Project one-liner placeholder
    idx_lines.append("## Overview")
    # Try to find the first generated purpose as project description
    first_purpose = ""
    for mod in skeleton.get("modules", []):
        ctx_data = existing.get(mod["id"], {})
        if ctx_data.get("purpose") and ctx_data["purpose"] != "UNKNOWN":
            first_purpose = ctx_data["purpose"]
            break
    if first_purpose:
        idx_lines.append(f"> {name} -- {first_purpose}")
    else:
        idx_lines.append(f"> {name} -- *Run ctx-gen agent to generate project description.*")
    idx_lines.append("")

    # Domain table
    idx_lines.append("## Domains")
    idx_lines.append("| Domain | Modules | Languages | Tags |")
    idx_lines.append("|--------|---------|-----------|------|")
    for d_name, d_info in sorted(domain_summary.items()):
        langs = ", ".join(d_info.get("languages", []))
        tags = ", ".join(f"`{t}`" for t in d_info.get("tags", [])[:8])
        count = d_info.get("module_count", 0)
        idx_lines.append(f"| [{d_name}](domains/{d_name}/) | {count} | {langs} | {tags} |")
    idx_lines.append("")

    # Quick lookup by tag
    idx_lines.append("## Tags")
    all_tags: Counter = Counter()
    for mod in skeleton.get("modules", []):
        for t in mod.get("tags", []):
            all_tags[t] += 1
    if all_tags:
        for tag, count in all_tags.most_common(30):
            matching = [m["id"] for m in skeleton.get("modules", [])
                        if tag in m.get("tags", [])]
            idx_lines.append(f"- **`{tag}`** ({count}): "
                             + ", ".join(matching[:8])
                             + ("..." if len(matching) > 8 else ""))
    else:
        idx_lines.append("*No tags detected. Run scan_skeleton first.*")
    idx_lines.append("")

    # Full module list per domain
    idx_lines.append("## Module List")
    for d_name in sorted(domain_summary.keys()):
        dom_mods = [m for m in skeleton.get("modules", [])
                    if m.get("domain") == d_name]
        idx_lines.append(f"### {d_name}")
        idx_lines.append("| Module | Verified | Files | Lines | Entry | Purpose | Tags |")
        idx_lines.append("|--------|----------|-------|-------|-------|---------|------|")
        for m in sorted(dom_mods, key=lambda x: x["id"]):
            mid = m["id"]
            ctx_data = existing.get(mid, {})
            purpose = (ctx_data.get("purpose") or "?")[:50]
            entry = m.get("entry", "-")
            tags_str = ", ".join(m.get("tags", [])[:5])
            verified = ctx_data.get("verified", False)
            v_mark = "OK" if verified else "--"
            idx_lines.append(
                f"| [{mid}](domains/{d_name}/{mid}.wiki.md) | "
                f"{v_mark} | "
                f"{m.get('file_count', 0)} | "
                f"{m.get('total_lines', 0)} | "
                f"`{entry}` | {purpose} | {tags_str} |"
            )
        idx_lines.append("")

    # Glossary section (if project has one)
    if glossary:
        idx_lines.append("## Project Glossary")
        idx_lines.append(
            "> Confirmed abbreviation expansions "
            "(sourced from `.ctx-cache/glossary.json`).\n"
        )
        for abbrev, meaning in sorted(glossary.items()):
            if meaning != "[UNKNOWN]":
                idx_lines.append(f"- **`{abbrev}`**: {meaning}")
        idx_lines.append("")

    idx_path = wiki_dir / "INDEX.md"
    idx_path.write_text("\n".join(idx_lines), encoding="utf-8")

    return {
        "index_doc": str(idx_path),
        "module_docs": module_docs,
        "domain_dirs": domain_dirs,
        "modules_processed": len(module_docs),
        "errors": errors,
        "glossary_errors": glossary_errors,
        "output_format": "wiki",
        "_fatal_errors": glossary_errors,  # non-empty = wiki may be unreliable
    }


def dep_domain(module_id: str, skeleton: dict) -> str:
    """Look up a module's domain from skeleton data."""
    for m in skeleton.get("modules", []):
        if m["id"] == module_id:
            return m.get("domain", "_root")
    return "_root"


# -- Hallucination Detection ---------------------------------------------------

_HALLUCINATION_PATTERNS = [
    # Chinese: "X 表示 Y" / "X 是 Y 的缩写" / "X 代表 Y"
    re.compile(r'[\u4e00-\u9fff]+\s*(表示|是.*缩写|代表)\s*[\u4e00-\u9fff]+', re.IGNORECASE),
    # English: "X stands for Y" / "X is a Y" / "X means Y"
    re.compile(r'\bstands\s+for\b', re.IGNORECASE),
    re.compile(r'\bis\s+a\s+[A-Z][a-z]+\b', re.IGNORECASE),
    re.compile(r'\bmeans\s+[a-z]+', re.IGNORECASE),
    # Common hallucinated patterns
    re.compile(r'\b(abbreviation|acronym)\s+(for|of)\b', re.IGNORECASE),
]


def _detect_hallucinations(ctx_data: dict) -> list[str]:
    """Scan ctx JSON for hallucination risks.

    Returns a list of warning strings.
    """
    warnings: list[str] = []
    anchors = ctx_data.get("source_anchors", {})
    mid = ctx_data.get("module_id", "unknown")

    # 1. Fields with substantial text but NO source anchor
    text_fields = {
        "purpose": ctx_data.get("purpose", ""),
        "design_notes": ctx_data.get("design_notes", ""),
        "disclosure_hint": ctx_data.get("disclosure_hint", ""),
    }
    anchor_fields = {
        "purpose": anchors.get("purpose", []),
        "design_notes": anchors.get("design_notes", []),
        "disclosure_hint": anchors.get("design_notes", []),  # often in design_notes anchor
    }
    for field_name, text in text_fields.items():
        if not text or text in ("UNKNOWN", "?", ""):
            continue
        if len(text) > 30 and not anchor_fields.get(field_name):
            warnings.append(
                f"[{mid}] Field `{field_name}` has {len(text)} chars of text "
                f"but NO source_anchor -- likely hallucinated."
            )

    # 2. Scan text for abbreviation explanation patterns without anchors
    combined_text = " ".join(str(v) for v in text_fields.values() if v)
    for pattern in _HALLUCINATION_PATTERNS:
        for match in pattern.finditer(combined_text):
            # Check if this match area has a nearby source anchor comment
            # (simple heuristic: if anchors exist for this field, OK; else warn)
            warnings.append(
                f"[{mid}] Possible abbreviation explanation detected: "
                f"...{match.group(0)[:60]}... -- verify with source code."
            )

    # 3. unknown_fields entries with "abbrev:" prefix -> user needs to verify
    unknown = ctx_data.get("unknown_fields", [])
    abbrev_unknown = [u for u in unknown if "abbrev" in str(u).lower()]
    for au in abbrev_unknown:
        warnings.append(f"[{mid}] Unverified abbreviation: {au}")

    return warnings


def _validate_source_anchors(ctx_data: dict, root: Path) -> list[str]:
    """Validate that source_anchors refer to real files and line numbers.

    Returns a list of error strings (empty = all anchors valid).
    """
    errors: list[str] = []
    anchors = ctx_data.get("source_anchors", {})
    mid = ctx_data.get("module_id", "unknown")

    all_anchor_refs: list[str] = []
    for v in anchors.values():
        if isinstance(v, list):
            all_anchor_refs.extend(v)
        elif isinstance(v, dict):
            all_anchor_refs.extend(v.values())

    for ref in all_anchor_refs:
        ref_str = str(ref)
        # Parse "file:line" or "file:line_start-line_end"
        m = re.match(r'^([^:]+):(\d+)(?:-(\d+))?$', ref_str)
        if not m:
            errors.append(f"[{mid}] Invalid source_anchor format: `{ref_str}`")
            continue
        file_rel = m.group(1)
        line_num = int(m.group(2))
        file_path = root / file_rel
        if not file_path.exists():
            errors.append(f"[{mid}] source_anchor references non-existent file: `{file_rel}`")
            continue
        # Check line number is within file bounds
        try:
            total_lines = _count_lines(file_path)
            if line_num > total_lines + 5:  # allow small offset
                errors.append(
                    f"[{mid}] source_anchor line {line_num} exceeds file length "
                    f"({total_lines} lines) in `{file_rel}`"
                )
        except Exception:
            pass  # skip if can't read

    return errors


def mcp_main():
    """Entry point for `ctx-gen-server` CLI."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    mcp_main()
