"""Outline → draft syllabus (BE-07 §1).

A learner creating a track may paste a rough outline. The backend never shapes a
syllabus (that is the model's job at the next sync), so this is only a
mechanical first cut so Track detail has rows at once: one item per non-empty
line, bullets and numbering stripped, indentation read as nesting. The outline
itself is kept on the agenda verbatim for the sync to tidy.

Pure functions, no I/O.
"""

from __future__ import annotations

import re

from jolt.domain.models import AgendaSource, SyllabusItem

MAX_OUTLINE_CHARS = 20_000
MAX_OUTLINE_ITEMS = 200
_BULLET = re.compile(r"^(?:[-*+•·–]|\d+[.)]|[a-zA-Z][.)](?=\s))\s*")
_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(label: str) -> str:
    slug = _SLUG.sub("-", label.lower()).strip("-")
    return slug[:80] or "item"


def _indent(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip(" "))


def outline_to_syllabus(outline: str) -> list[SyllabusItem]:
    """One draft item per outline line; nesting from indentation."""
    items: list[SyllabusItem] = []
    keys: set[str] = set()
    stack: list[tuple[int, str]] = []  # (indent, concept_key) of open parents
    for raw in outline.splitlines():
        if not raw.strip():
            continue
        indent = _indent(raw)
        label = _BULLET.sub("", raw.strip()).strip().rstrip(":").strip()
        if not label:
            continue
        label = label[:200]
        key = base = slugify(label)
        n = 2
        while key in keys:
            key = f"{base}-{n}"
            n += 1
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else None
        items.append(
            SyllabusItem(concept_key=key, label=label, parent=parent, source=AgendaSource.USER)
        )
        keys.add(key)
        stack.append((indent, key))
        if len(items) >= MAX_OUTLINE_ITEMS:
            break
    return items
