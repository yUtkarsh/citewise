"""Versioned Claude prompts: one file per prompt, named ``<name>_v<N>.md``.

``load("profile_ranges")`` returns the highest version on disk; the version is part
of every cache key, so editing a prompt means adding a new version file.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

PROMPT_DIR = Path(__file__).resolve().parent
VERSION_RE = re.compile(r"^(?P<name>.+)_v(?P<version>\d+)\.md$")


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str


def load(name: str) -> Prompt:
    versions = []
    for path in PROMPT_DIR.glob(f"{name}_v*.md"):
        match = VERSION_RE.match(path.name)
        if match and match.group("name") == name:
            versions.append((int(match.group("version")), path))
    if not versions:
        raise FileNotFoundError(f"no prompt file {name}_v<N>.md in {PROMPT_DIR}")
    version, path = max(versions)
    return Prompt(name=name, version=f"v{version}", text=path.read_text(encoding="utf-8"))
