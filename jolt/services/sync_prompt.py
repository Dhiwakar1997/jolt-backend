"""The nightly scheduled-task prompt (BE-04, decision D5).

The text lives in `docs/NIGHTLY_SYNC_PROMPT.md` (one source for the repo docs,
the front end's copy and `GET /v1/sync/prompt`). The served prompt is the part
between the `prompt:start` / `prompt:end` markers; its version is the
`prompt-version` comment. `{{RUBRIC_VERSION}}` is filled in from the grading
rubric so the two never drift. The Docker image copies the file (see
Dockerfile / .dockerignore).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from jolt.mcp.descriptions.rubric import RUBRIC_VERSION

PROMPT_PATH = Path(__file__).resolve().parents[2] / "docs" / "NIGHTLY_SYNC_PROMPT.md"

_VERSION = re.compile(r"<!--\s*prompt-version:\s*(?P<v>[^\s]+)\s*-->")
_BODY = re.compile(r"<!--\s*prompt:start\s*-->(?P<body>.*?)<!--\s*prompt:end\s*-->", re.DOTALL)


@dataclass(frozen=True)
class SyncPrompt:
    prompt: str
    version: str


def parse_prompt(text: str) -> SyncPrompt:
    version = _VERSION.search(text)
    body = _BODY.search(text)
    if not version or not body:
        raise ValueError("NIGHTLY_SYNC_PROMPT.md needs prompt-version and prompt:start/end markers")
    prompt = body.group("body").strip().replace("{{RUBRIC_VERSION}}", RUBRIC_VERSION)
    return SyncPrompt(prompt=prompt + "\n", version=version.group("v"))


@lru_cache
def load_prompt(path: Path = PROMPT_PATH) -> SyncPrompt:
    return parse_prompt(path.read_text(encoding="utf-8"))
