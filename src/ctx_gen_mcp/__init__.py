"""ctx_gen_mcp — package init."""

__version__ = "1.0.0"

def get_skill():
    """Return skill metadata for OpenCode discovery (if supported)."""
    import importlib.resources as pkg_resources
    from pathlib import Path
    skill_path = pkg_resources.files("ctx_gen_mcp") / "skill.md"
    return skill_path.read_text()
