"""Staff personas: the skills in ``skills/staff``, one per role.

Seed's Staff page reads and edits them through the API server. A persona is
off when its name is in the global ``skills.disabled`` list, so no agent,
routine, or subagent can load it.
"""
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.skill_utils import parse_frontmatter
from hermes_cli.config import load_config, save_config
from hermes_cli.skills_config import get_disabled_skills, save_disabled_skills
from hermes_constants import get_hermes_home

MAX_CONTENT_LENGTH = 20_000


def staff_dir() -> Path:
    return get_hermes_home() / "skills" / "staff"


def list_staff() -> List[Dict[str, Any]]:
    disabled = get_disabled_skills(load_config())
    return [_persona(path.parent.name, path.read_text(), disabled)
            for path in sorted(staff_dir().glob("*/SKILL.md"))]


def save_staff(name: str, content: Optional[str] = None, enabled: Optional[bool] = None) -> Dict[str, Any]:
    """Update a persona's SKILL.md and/or its on/off state. Raises KeyError for
    an unknown persona and ValueError for content that is not a valid skill."""
    path = staff_dir() / name / "SKILL.md"
    if "/" in name or name.startswith(".") or not path.is_file():
        raise KeyError(name)
    if content is not None:
        if len(content) > MAX_CONTENT_LENGTH:
            raise ValueError(f"Content must be at most {MAX_CONTENT_LENGTH} characters")
        meta, body = parse_frontmatter(content)
        if meta.get("name") != name or not meta.get("description") or not body.strip():
            raise ValueError(f"Content needs frontmatter with name: {name}, a description, and a body")
        path.write_text(content if content.endswith("\n") else content + "\n")
        os.utime(staff_dir())  # the skills listing cache keys on category mtimes
    config = load_config()
    disabled = get_disabled_skills(config)
    if enabled is not None:
        wanted = disabled - {name} if enabled else disabled | {name}
        if wanted != disabled:
            save_disabled_skills(config, wanted)
            disabled = wanted
    return _persona(name, path.read_text(), disabled)


def _persona(name: str, content: str, disabled) -> Dict[str, Any]:
    meta, _ = parse_frontmatter(content)
    return {"name": name, "description": meta.get("description", ""), "content": content,
            "enabled": name not in disabled}
