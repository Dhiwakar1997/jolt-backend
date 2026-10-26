"""A small synthetic Jolt checkout for the BE-09 import tests (not a test module).

Four patterns, ten playable problems and one still pending, laid out like the
real repo (`data/archived/*.json`, `src/tracks/dsa/content/*.ts`), so the
importer runs exactly as it does against Jolt.
"""

from __future__ import annotations

import json
from pathlib import Path

PATTERNS = ["Arrays & Hashing", "Two Pointers", "Binary Search", "Stack"]
WEIGHT = {"Easy": 2, "Medium": 4, "Hard": 7}

# (leetcodeId, difficulty, algorithm, acceptable, nearDuplicateOf)
PROBLEMS = [
    (1, "Easy", "Arrays & Hashing", [], None),
    (2, "Easy", "Arrays & Hashing", [], None),
    (3, "Medium", "Two Pointers", ["Arrays & Hashing"], None),
    (4, "Medium", "Two Pointers", [], None),
    (5, "Hard", "Binary Search", [], None),
    (6, "Medium", "Binary Search", [], None),
    (7, "Medium", "Binary Search", [], 6),
    (8, "Easy", "Stack", [], None),
    (9, "Medium", "Stack", ["Two Pointers"], None),
    (10, "Hard", "Stack", [], None),
]
PENDING_ID = 11

SENTENCE = (
    "A depot clerk logs each parcel as it arrives and wants a quick answer at the end of the shift."
)


def title_of(lc: int) -> str:
    return f"Quokka Riddle {lc}"


def description_of(lc: int) -> str:
    return f"Case {lc}. " + " ".join([SENTENCE] * 7)


def problem(lc, difficulty, algorithm, acceptable, near) -> dict:
    p = {
        "id": f"p{lc:03d}",
        "leetcodeId": lc,
        "title": title_of(lc),
        "difficulty": difficulty,
        "url": f"https://leetcode.com/problems/quokka-riddle-{lc}/",
        "algorithm": algorithm,
        "acceptableAlgorithms": acceptable,
        "timeWeightMinutes": WEIGHT[difficulty],
        "description": description_of(lc),
        "descriptionStatus": "original",
        "io": {
            "input": f"A list of parcel numbers for case {lc}.",
            "output": "A whole number.",
            "examples": [
                {"input": "[1, 2]", "output": "1", "explanation": "The first one."},
                {"input": "[3]", "output": "0", "explanation": "Nothing to report."},
            ],
        },
        "ioStatus": "original",
    }
    p["wordCount"] = len(p["description"].split())
    if near is not None:
        p["nearDuplicateOf"] = near
    return p


def batch(problems: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for p in problems:
        counts[p["algorithm"]] = counts.get(p["algorithm"], 0) + 1
    return {
        "schemaVersion": 1,
        "algorithms": sorted(PATTERNS),
        "counts": counts,
        "totalProblems": len(problems),
        "descriptionsFilled": sum(1 for p in problems if p.get("description")),
        "ioFilled": sum(1 for p in problems if p.get("io")),
        "problems": problems,
    }


LIBRARY_TS = """// Library content (fixture).
export interface LibraryEntry { category: string; }

export const LIBRARY: Record<string, LibraryEntry> = {
  'Arrays & Hashing': {
    category: 'Arrays / Strings',
    time: 'O(n)',
    space: 'O(n)',
    whenToReach: 'You need fast membership, counts, or grouping.',
    tells: ['“has duplicates”, “count of”', 'seen-before checks'],
    pseudocode: ['seen = {}', 'for x in a:', '  seen[x] = 1'],
    explanation: ['Remember everything as you go.'],
  },
  'Two Pointers': {
    category: "Arrays / Strings",
    time: 'O(n)',
    space: 'O(1)',
    whenToReach: 'A sorted or paired structure where two indices converge.',
    tells: ['“sorted array”'],
    pseudocode: ['i, j = 0, n-1'],
    explanation: [`Each step drops a candidate.`],
  },
  'Binary Search': {
    category: 'Arrays / Search',
    time: 'O(log n)',
    space: 'O(1)',
    whenToReach: 'A sorted input, or a monotonic answer space.',
    tells: ['“minimum value such that …”'],
    pseudocode: ['lo, hi = 0, n-1'],
    explanation: ['Halve what remains.'],
  },
  Stack: {
    category: 'Arrays / Strings',
    time: 'O(n)',
    space: 'O(n)',
    whenToReach: 'Next-greater, matched brackets, nesting.',
    tells: ['“next greater”', 'it\\'s nested'],
    pseudocode: ['st = []'],
    explanation: ['Resolve lazily.'],
  },
};

export function libraryEntry(tag: string): LibraryEntry | undefined {
  return LIBRARY[tag];
}
"""

TREE_TS = """import { algoSlug } from '@/tracks/dsa/lib/slug';

export const TREE_ROOT = 'q_sorted';

export const TREE: Record<string, TreeNode> = {
  q_sorted: {
    id: 'q_sorted',
    kind: 'question',
    prompt: 'Is the input sorted?',
    short: 'Sorted?',
    yes: 'leaf_binary',
    no: 'q_nesting',
  },
  q_nesting: {
    id: 'q_nesting', kind: 'question', prompt: 'Nesting?', short: 'Nesting?',
    yes: 'leaf_stack', no: 'leaf_hashing',
  },
  leaf_binary: { id: 'leaf_binary', kind: 'leaf', pattern: 'Binary Search', note: 'Halve it.' },
  leaf_stack: { id: 'leaf_stack', kind: 'leaf', pattern: 'Stack (monotonic)', note: 'Pop lazily.' },
  leaf_hashing: { id: 'leaf_hashing', kind: 'leaf', pattern: 'Arrays & Hashing', note: 'Count.' },
};
"""

ANIMATIONS_TS = """const c = (v) => v;
export const ANIMATIONS: Record<string, Reel> = {
  'Arrays & Hashing': {
    title: 'Two Sum',
    frames: [c(1)],
  },
  'Binary Search': {
    title: 'Search',
    frames: [c(2)],
  },
  Stack: {
    title: 'Stack',
    frames: [c(3)],
  },
};
"""


def write_repo(
    root: Path, *, problems: list[dict] | None = None, new: list[dict] | None = None
) -> Path:
    """Write the fixture checkout under `root` and return it."""
    if problems is None:
        problems = [problem(*row) for row in PROBLEMS]
        problems.append(
            {
                "id": f"p{PENDING_ID:03d}",
                "leetcodeId": PENDING_ID,
                "title": title_of(PENDING_ID),
                "difficulty": "Easy",
                "url": "https://leetcode.com/problems/quokka-riddle-11/",
                "algorithm": "Stack",
                "acceptableAlgorithms": [],
                "timeWeightMinutes": 2,
                "description": None,
                "descriptionStatus": "pending_original_authoring",
            }
        )
    archived = root / "data" / "archived"
    archived.mkdir(parents=True, exist_ok=True)
    (archived / "batch-001.json").write_text(json.dumps(batch(problems), indent=1))
    if new is not None:
        (root / "data" / "new").mkdir(parents=True, exist_ok=True)
        (root / "data" / "new" / "batch-002.json").write_text(json.dumps(batch(new), indent=1))
    content = root / "src" / "tracks" / "dsa" / "content"
    content.mkdir(parents=True, exist_ok=True)
    (content / "library.ts").write_text(LIBRARY_TS)
    (content / "decisionTree.ts").write_text(TREE_TS)
    (content / "animations.ts").write_text(ANIMATIONS_TS)
    return root
