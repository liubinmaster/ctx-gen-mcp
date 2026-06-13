"""ctx_gen_mcp/install.py -- One-click setup script.

Usage:
  ctx-gen-setup                    # install to current project
  ctx-gen-setup --global          # install to ~/.config/opencode/
  ctx-gen-setup --project-dir .  # explicit project root
  ctx-gen-setup --uninstall      # remove config
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


# ── Package data helpers ──────────────────────────────────────────────────

def _read_pkg(name: str) -> str:
    """Read a bundled package data file."""
    import importlib.resources as pkg
    ref = pkg.files("ctx_gen_mcp") / name
    return ref.read_text(encoding="utf-8")


# ── Config discovery ───────────────────────────────────────────────────────

def find_opencode_dir(project_dir: Path, global_: bool) -> Path:
    """Find or create the OpenCode config directory."""
    if global_:
        p = Path.home() / ".config" / "opencode"
        p.mkdir(parents=True, exist_ok=True)
        return p
    d = project_dir.resolve()
    for parent in [d] + list(d.parents):
        if (parent / ".git").exists() or (parent / ".opencode").exists():
            d = parent
            break
    oc = d / ".opencode"
    oc.mkdir(exist_ok=True)
    return oc


def read_opencode_json(oc_dir: Path) -> dict[str, Any]:
    p = oc_dir / "opencode.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def write_opencode_json(oc_dir: Path, data: dict[str, Any]):
    p = oc_dir / "opencode.json"
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Install steps ─────────────────────────────────────────────────────────

def install_skill(oc_dir: Path):
    skills_dir = oc_dir / "skills" / "ctx-gen"
    skills_dir.mkdir(parents=True, exist_ok=True)
    content = _read_pkg("skill.md")
    (skills_dir / "SKILL.md").write_text(content, encoding="utf-8")
    print(f"  [OK] Skill installed: {skills_dir / 'SKILL.md'}")


def install_agent(oc_dir: Path):
    agents_dir = oc_dir / "agents"
    agents_dir.mkdir(exist_ok=True)
    content = _read_pkg("agent.md")
    (agents_dir / "ctx-gen.md").write_text(content, encoding="utf-8")
    print(f"  [OK] Agent installed: {agents_dir / 'ctx-gen.md'}")


def install_agents_md(project_dir: Path):
    ag = project_dir / "AGENTS.md"
    content = _read_pkg("AGENTS_md_template.md")
    if ag.exists():
        existing = ag.read_text(encoding="utf-8")
        if "CTX-GEN INSTRUCTIONS" in existing:
            print("  [SKIP] AGENTS.md already has ctx-gen section")
            return
        ag.write_text(existing + "\n\n" + content, encoding="utf-8")
        print("  [OK] AGENTS.md updated (appended)")
    else:
        ag.write_text(content, encoding="utf-8")
        print(f"  [OK] AGENTS.md created: {ag}")


def _find_mcp_command() -> tuple[str, list[str]]:
    """Find the MCP server command."""
    import shutil as sh
    if sh.which("ctx-gen-server"):
        return "ctx-gen-server", []
    return sys.executable, ["-m", "ctx_gen_mcp.server"]


def configure_mcp(oc_dir: Path):
    """Add ctx-gen MCP server to opencode.json."""
    cfg = read_opencode_json(oc_dir)
    cmd, args = _find_mcp_command()

    mcp_entry = {
        "command": cmd,
        "args": args,
        "env": {},
        "description": "Code context description generator -- deterministic tools for progressive docs",
    }

    if "mcpServers" not in cfg:
        cfg["mcpServers"] = {}
    cfg["mcpServers"]["ctx-gen"] = mcp_entry

    # Also add skill permission
    if "permission" not in cfg:
        cfg["permission"] = {}
    if "skill" not in cfg["permission"]:
        cfg["permission"]["skill"] = {}
    cfg["permission"]["skill"]["ctx-gen"] = "allow"

    write_opencode_json(oc_dir, cfg)
    print(f"  [OK] MCP server configured in {oc_dir / 'opencode.json'}")
    print(f"       command: {cmd} {' '.join(args)}")


def print_post_install(oc_dir: Path, project_dir: Path):
    print()
    print("=" * 60)
    print("  ctx-gen plugin installed successfully!")
    print("=" * 60)
    print()
    print("  Next steps:")
    print("  1. Restart OpenCode to load the new MCP server + skill")
    print(f"  2. Open project: {project_dir}")
    print("  3. In OpenCode, say: 'use the ctx-gen skill to generate context'")
    print("  4. Or switch to the 'ctx-gen' agent in the agent panel")
    print()
    print("  Files installed:")
    print(f"  - Skill:    {oc_dir / 'skills' / 'ctx-gen' / 'SKILL.md'}")
    print(f"  - Agent:    {oc_dir / 'agents' / 'ctx-gen.md'}")
    print(f"  - AGENTS.md: {project_dir / 'AGENTS.md'}")
    print(f"  - MCP config: {oc_dir / 'opencode.json'}")
    print()
    print("  To test MCP server manually:")
    print(f"    ctx-gen-server")
    print(f"    # or: python -m ctx_gen_mcp.server")
    print("=" * 60)


# ── Uninstall ─────────────────────────────────────────────────────────────

def uninstall(oc_dir: Path, project_dir: Path):
    """Remove ctx-gen from OpenCode config."""
    skill_dir = oc_dir / "skills" / "ctx-gen"
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
        print(f"  [OK] Removed skill: {skill_dir}")

    agent_file = oc_dir / "agents" / "ctx-gen.md"
    if agent_file.exists():
        agent_file.unlink()
        print(f"  [OK] Removed agent: {agent_file}")

    cfg = read_opencode_json(oc_dir)
    changed = False
    if "mcpServers" in cfg and "ctx-gen" in cfg["mcpServers"]:
        del cfg["mcpServers"]["ctx-gen"]
        changed = True
    if changed:
        write_opencode_json(oc_dir, cfg)
        print("  [OK] Removed MCP config from opencode.json")

    ag = project_dir / "AGENTS.md"
    if ag.exists() and "CTX-GEN INSTRUCTIONS" in ag.read_text(encoding="utf-8"):
        print("  [NOTE] AGENTS.md still has ctx-gen section -- remove manually if desired")
    print("  Uninstalled.")


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ctx-gen: one-click setup for OpenCode plugin"
    )
    parser.add_argument(
        "--project-dir", "-p", default=".",
        help="Project root directory (default: cwd)",
    )
    parser.add_argument(
        "--global", dest="global_", action="store_true",
        help="Install to ~/.config/opencode/ (all projects)",
    )
    parser.add_argument(
        "--uninstall", "-u", action="store_true",
        help="Remove ctx-gen from OpenCode config",
    )
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    oc_dir = find_opencode_dir(project_dir, args.global_)

    print(f"\n[ctx-gen] OpenCode dir: {oc_dir}")
    print(f"           Project dir:  {project_dir}\n")

    if args.uninstall:
        uninstall(oc_dir, project_dir)
        return

    # Install
    print("[1/4] Installing skill...")
    install_skill(oc_dir)
    print("[2/4] Installing agent...")
    install_agent(oc_dir)
    print("[3/4] Creating AGENTS.md...")
    install_agents_md(project_dir)
    print("[4/4] Configuring MCP server...")
    configure_mcp(oc_dir)

    print_post_install(oc_dir, project_dir)


if __name__ == "__main__":
    main()
