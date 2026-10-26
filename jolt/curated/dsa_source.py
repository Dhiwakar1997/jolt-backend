"""Read and validate the v1 DSA content from a Jolt checkout (BE-09).

Sources, relative to the Jolt repo root:

* `data/archived/*.json` — the seeded problem batches; with `include_new`, also
  `data/new/*.json`. Only problems whose `descriptionStatus` is `original` are
  playable and imported; the rest are counted as skipped.
* `src/tracks/dsa/content/library.ts` (`LIBRARY`), `decisionTree.ts` (`TREE`,
  `TREE_ROOT`) and `animations.ts` (which patterns have a step animation) — read
  with `jolt.curated.ts_literal`, or taken from a JSON export (`npm run
  export:dsa` in Jolt; `scripts/export_dsa_from_jolt.mjs` here) when one is given.

Validation mirrors Jolt's `scripts/check-batch.mjs`: the same errors (missing
metadata, off-catalog tags, wrong time weights, banned terms, placeholder text,
word counts, I/O contract shape, header counts, the tag set drifting between
batches) and the same warnings (soft tells, title echoes, missing explanations),
plus what the import needs: every tag has a library entry, leetcode ids are
unique across the imported batches, and near-duplicate links point at a known
problem. Any error stops the import; warnings are reported.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from jolt.curated.ts_literal import read_export

WORDS_MIN = 120
WORDS_MAX = 200
EXAMPLES_MIN = 2
EXAMPLES_MAX = 3
TIME_WEIGHT = {"Easy": 2, "Medium": 4, "Hard": 7}
REQUIRED_FIELDS = (
    "id",
    "leetcodeId",
    "title",
    "difficulty",
    "url",
    "algorithm",
    "timeWeightMinutes",
)

LIBRARY_TS = Path("src/tracks/dsa/content/library.ts")
TREE_TS = Path("src/tracks/dsa/content/decisionTree.ts")
ANIMATIONS_TS = Path("src/tracks/dsa/content/animations.ts")

_I = re.IGNORECASE
# check-batch.mjs BANNED: textbook names for a technique (errors).
BANNED = [
    re.compile(p, _I)
    for p in (
        r"\bdynamic program\w*",
        r"\bmemo(?:ise|ize|isation|ization)\w*",
        r"\bsubproblem",
        r"\bbinary search\b",
        r"\btwo[- ]pointer",
        r"\bsliding window\b",
        r"\bfast and slow\b",
        r"\bbacktrack\w*",
        r"\bbrute[- ]force\b",
        r"\bdivide and conquer\b",
        r"\bhash\w*",
        r"\bhashmap\b",
        r"\btrie\b",
        r"\bprefix tree\b",
        r"\bpriority queue\b",
        r"\bmin[- ]heap\b",
        r"\bmax[- ]heap\b",
        r"\bheapif\w*",
        r"\bbreadth[- ]first\b",
        r"\bdepth[- ]first\b",
        r"\bbfs\b",
        r"\bdfs\b",
        r"\btopolog\w*",
        r"\bdijkstra\b",
        r"\bbellman[- ]ford\b",
        r"\bkruskal\b",
        r"\bprim'?s\b",
        r"\bspanning tree\b",
        r"\bmst\b",
        r"\beuler(?:ian)?\b",
        r"\bhierholzer\b",
        r"\bkadane\w*",
        r"\bunion[- ]find\b",
        r"\bdisjoint set\b",
        r"\bmonotonic\b",
        r"\bbit ?mask\w*",
        r"\bbitwise\b",
        r"\btime complexity\b",
        r"\bspace complexity\b",
        r"\bbig[- ]o\b",
        r"\bamortis\w*|\bamortiz\w*",
        r"\balgorithm",
        r"\bdata structure\b",
        r"\brecursi\w*",
        r"\boptimal substructure\b",
        r"\bleetcode\b",
        r"\bthis (?:problem|exercise|question|challenge)\b",
    )
] + [re.compile(r"\bO\([n1m\s^*log]+\)")]

# check-batch.mjs SOFT: real English that is also a tell for some tag (warnings).
SOFT: dict[str, list[re.Pattern]] = {
    tag: [re.compile(p, _I) for p in pats]
    for tag, pats in {
        "Stack": [r"\bstack\b", r"\blast[- ]in\b"],
        "Heap / Priority Queue": [r"\bheap\b", r"\bpriorit\w*"],
        "Trees": [r"\bsubtree\b", r"\broot node\b"],
        "Tries": [r"\bprefix\b"],
        "DFS": [r"\bgraph\b", r"\bvertex\b|\bvertices\b", r"\badjacency\b"],
        "BFS": [r"\bgraph\b", r"\bvertex\b|\bvertices\b", r"\bfewest (?:steps|moves|hops)\b"],
        "Dijkstra": [r"\bgraph\b", r"\bshortest path\b", r"\bcheapest route\b"],
        "Bellman-Ford": [
            r"\bgraph\b",
            r"\bshortest path\b",
            r"\bnegative (?:cost|weight|value)\w*",
        ],
        "Topological Sort": [r"\bprerequisite\w*", r"\bdependenc\w*"],
        "Union-Find": [r"\bgraph\b", r"\bconnected component\w*", r"\bmerge\w? (?:the )?groups\b"],
        "MST": [r"\bgraph\b", r"\bconnect (?:them |everything |all )\w*\b"],
        "Eulerian Path": [r"\bgraph\b", r"\bevery (?:edge|leg|flight|route) exactly once\b"],
        "Linked List": [r"\blinked list\b", r"\bnext pointer\b"],
        "Intervals": [r"\binterval\b"],
        "Binary Search": [r"\bhalve\b|\bhalving\b", r"\bmidpoint\b"],
        "Sliding Window": [r"\bwindow\b"],
        "Two Pointers": [r"\bpointer\b"],
        "Bit Manipulation": [r"\bbinary representation\b"],
        "Backtracking": [r"\bevery possible combination\b", r"\bexhaustive\w*"],
        "Greedy": [r"\bgreedy\b", r"\blocally optimal\b"],
    }.items()
}

PLACEHOLDERS = [
    re.compile(p, f)
    for p, f in (
        (r"\bTODO\b", _I),
        (r"\bTBD\b", _I),
        (r"\bFIXME\b", _I),
        (r"\bXXX\b", 0),
        (r"\blorem ipsum\b", _I),
        (r"\bplaceholder\b", _I),
        (r"\bdescription (?:here|goes here|pending)\b", _I),
        (r"\b(?:the )?rest (?:follow|are) the same\b", _I),
        (r"\bsame (?:as|pattern as) above\b", _I),
        (r"\betc\.?\s*$", _I),
        (r"\.\.\.\s*$", 0),
    )
]

STOP = set(
    "the and for with from into that this than then all any number numbers array arrays string "
    "strings list lists binary maximum minimum longest shortest valid best time two three four "
    "sum of in a an to k i ii iii iv".split()
)


class SourceError(Exception):
    """The Jolt checkout is missing a file the import needs, or it does not parse."""


def pattern_slug(tag: str) -> str:
    """v1 `algoSlug`: the stable key of a pattern (syllabus key, animation id)."""
    s = tag.lower().replace("&", "and").replace("/", " ")
    return re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", s))


@dataclass
class Problem:
    """One playable v1 problem, as the import stores it."""

    leetcode_id: int
    title: str
    difficulty: str
    url: str
    algorithm: str
    acceptable: list[str]
    time_weight: int
    description: str
    io_input: str
    io_output: str
    examples: list[dict]
    near_duplicate_of: Optional[int]
    batch: str


@dataclass
class DsaBundle:
    """Everything the import writes, validated."""

    patterns: list[str]  # library order: the syllabus and the answer grid
    library: dict[str, dict]
    animations: set[str]
    decision_tree: dict  # {root, nodes}
    problems: list[Problem]
    batches: list[str]
    skipped: int = 0  # problems not playable (descriptionStatus != original)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)  # soft tell on the problem's own tag

    @property
    def ok(self) -> bool:
        return not self.errors


def _first(pats: list[re.Pattern], text: str) -> Optional[str]:
    for p in pats:
        m = p.search(text)
        if m:
            return m.group(0)
    return None


def check_batch(
    name: str,
    batch: dict,
    foreign_ids: dict[int, str],
    foreign_tags: dict[str, list[str]],
) -> tuple[list[str], list[str], list[str], int, int]:
    """Port of check-batch.mjs for one batch: (errors, warnings, review, authored, pending)."""
    errors: list[str] = []
    warnings: list[str] = []
    review: list[str] = []
    tags = set(batch.get("algorithms") or [])
    seen: set = set()
    authored = pending = 0
    problems = batch.get("problems") or []

    for p in problems:
        at = f"{name} {p.get('leetcodeId')} {p.get('title')}"

        def err(m: str) -> None:
            errors.append(f"{at}: {m}")

        def warn(m: str) -> None:
            warnings.append(f"{at}: {m}")

        lc = p.get("leetcodeId")
        if lc in seen:
            err("duplicate leetcodeId within this batch")
        seen.add(lc)
        if lc in foreign_ids:
            err(f"already seeded in {foreign_ids[lc]}")
        for f in REQUIRED_FIELDS:
            if p.get(f) is None:
                err(f'missing required metadata field "{f}"')
        algo = p.get("algorithm")
        if algo is not None and algo not in tags:
            err(f'primary algorithm "{algo}" is not in algorithms[]')
        acceptable = p.get("acceptableAlgorithms")
        if not isinstance(acceptable, list):
            err("acceptableAlgorithms must be an array")
            acceptable = []
        if algo in acceptable:
            err("acceptableAlgorithms repeats the primary algorithm")
        for tag in acceptable:
            if tag not in tags:
                err(f'acceptableAlgorithms has off-catalog tag "{tag}"')
        expected = TIME_WEIGHT.get(p.get("difficulty"))
        if expected is None:
            err(f'unknown difficulty "{p.get("difficulty")}"')
        elif p.get("timeWeightMinutes") != expected:
            err(
                f"timeWeightMinutes is {p.get('timeWeightMinutes')}, expected {expected} "
                f"for {p.get('difficulty')}"
            )

        description = p.get("description")
        if description is None or str(description).strip() == "":
            pending += 1
            if p.get("descriptionStatus") != "pending_original_authoring":
                err(f'no description but descriptionStatus is "{p.get("descriptionStatus")}"')
            continue
        authored += 1
        if p.get("descriptionStatus") != "original":
            err(
                f'description is written but descriptionStatus is "{p.get("descriptionStatus")}"'
                ' — set it to "original"'
            )
        words = len(str(description).strip().split())
        if words < WORDS_MIN or words > WORDS_MAX:
            err(f"description is {words} words, want {WORDS_MIN}–{WORDS_MAX}")
        if p.get("wordCount") is not None and abs(p["wordCount"] - words) > 2:
            err(f"wordCount says {p['wordCount']} but the text is {words} words")
        if p.get("wordCount") is None:
            warn("no wordCount field")

        io = p.get("io") or None
        prose = "\n".join(
            str(x)
            for x in [
                description,
                (io or {}).get("input"),
                (io or {}).get("output"),
                *[e.get("explanation") for e in (io or {}).get("examples") or []],
            ]
            if x
        )
        hit = _first(PLACEHOLDERS, prose)
        if hit:
            err(f'placeholder text "{hit.strip()}" — this problem is not actually authored')
        for pat in BANNED:
            m = pat.search(prose)
            if m:
                err(f'leaks banned term "{m.group(0)}"')
        for pat in SOFT.get(algo, []):
            m = pat.search(prose)
            if m:
                review.append(f'{at}: uses "{m.group(0)}" — its own tag is {algo}.')
        for tag, pats in SOFT.items():
            if tag == algo:
                continue
            for pat in pats:
                m = pat.search(prose)
                if m:
                    warn(f'uses "{m.group(0)}" (a tell for {tag}) — fine if the scenario needs it')
        title_words = [
            w
            for w in re.split(r"[^a-z0-9]+", str(p.get("title", "")).lower())
            if len(w) >= 5 and w not in STOP
        ]
        echoed = [w for w in title_words if w in str(description).lower()]
        if echoed:
            warn(f"description echoes title word(s): {', '.join(echoed)}")

        if not io:
            err("description is authored but io is missing")
        else:
            if p.get("ioStatus") != "original":
                err(f'io is written but ioStatus is "{p.get("ioStatus")}"')
            if not str(io.get("input") or "").strip():
                err("io.input is empty")
            if not str(io.get("output") or "").strip():
                err("io.output is empty")
            ex = io.get("examples")
            if not isinstance(ex, list) or not EXAMPLES_MIN <= len(ex) <= EXAMPLES_MAX:
                err(
                    f"io.examples has {len(ex) if isinstance(ex, list) else 'none'}, "
                    f"want {EXAMPLES_MIN}–{EXAMPLES_MAX}"
                )
                ex = ex if isinstance(ex, list) else []
            for i, e in enumerate(ex):
                if not str(e.get("input") or "").strip():
                    err(f"io.examples[{i}].input is empty")
                if not str(e.get("output") or "").strip():
                    err(f"io.examples[{i}].output is empty")
                if not str(e.get("explanation") or "").strip():
                    warn(f"io.examples[{i}] has no explanation")
            if len(ex) >= 2 and len({str(e.get("output")) for e in ex}) == 1:
                warn("every example has the same output — add one that discriminates")

    if batch.get("totalProblems") != len(problems):
        errors.append(
            f"{name}: header totalProblems is {batch.get('totalProblems')} "
            f"but problems[] has {len(problems)}"
        )
    if batch.get("descriptionsFilled") != authored:
        errors.append(
            f"{name}: header descriptionsFilled is {batch.get('descriptionsFilled')} "
            f"but {authored} are authored"
        )
    io_authored = sum(1 for p in problems if p.get("io"))
    if batch.get("ioFilled") != io_authored:
        errors.append(
            f"{name}: header ioFilled is {batch.get('ioFilled')} but {io_authored} have io"
        )
    actual: dict[str, int] = {}
    for p in problems:
        actual[p.get("algorithm")] = actual.get(p.get("algorithm"), 0) + 1
    declared_counts = batch.get("counts") or {}
    for tag in set(declared_counts) | set(actual):
        if declared_counts.get(tag, 0) != actual.get(tag, 0):
            errors.append(
                f'{name}: header counts["{tag}"] is {declared_counts.get(tag, 0)} '
                f"but {actual.get(tag, 0)} problems carry that tag"
            )
    for other_name, other in foreign_tags.items():
        added = [t for t in batch.get("algorithms") or [] if t not in other]
        dropped = [t for t in other if t not in tags]
        if added:
            errors.append(f"{name}: algorithms[] adds {', '.join(added)} — not in {other_name}.")
        if dropped:
            errors.append(
                f"{name}: algorithms[] drops {', '.join(dropped)} — still in {other_name}."
            )
    return errors, warnings, review, authored, pending


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceError(f"missing {path}") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f"{path} is not valid JSON: {exc}") from exc


def _read_ts_content(repo: Path) -> dict:
    """LIBRARY, TREE, TREE_ROOT and the animated patterns, from the TS sources."""
    try:
        library_src = (repo / LIBRARY_TS).read_text(encoding="utf-8")
        tree_src = (repo / TREE_TS).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SourceError(f"missing {exc.filename}") from exc
    try:
        library = read_export(library_src, "LIBRARY")
        tree = {"root": read_export(tree_src, "TREE_ROOT"), "nodes": read_export(tree_src, "TREE")}
    except ValueError as exc:
        raise SourceError(f"cannot read the DSA content: {exc}") from exc
    animations: list[str] = []
    anim_path = repo / ANIMATIONS_TS
    if anim_path.exists():
        # ANIMATIONS is built with helper calls, so only its keys are read: the
        # top-level `  'Tag': {` / `  Tag: {` lines of the object.
        text = anim_path.read_text(encoding="utf-8")
        start = text.find("export const ANIMATIONS")
        body = text[start:] if start >= 0 else ""
        for m in re.finditer(r"^  (?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_$][\w$]*)): \{", body, re.M):
            animations.append(m.group(1) or m.group(2) or m.group(3))
    return {"library": library, "decisionTree": tree, "animations": animations}


def load_bundle(
    repo: Path | str,
    *,
    include_new: bool = False,
    content_json: Optional[Path | str] = None,
) -> DsaBundle:
    """Read the Jolt checkout at `repo` and validate it. Raises `SourceError` when a
    needed file is missing or unreadable; validation problems land in `errors`."""
    repo = Path(repo)
    content = _read_json(Path(content_json)) if content_json is not None else _read_ts_content(repo)
    library: dict[str, dict] = content.get("library") or {}
    tree: dict = content.get("decisionTree") or {}
    animations = set(content.get("animations") or [])

    archived_dir = repo / "data" / "archived"
    if not archived_dir.is_dir():
        raise SourceError(f"missing {archived_dir}")
    archived = sorted(archived_dir.glob("*.json"))
    new = sorted((repo / "data" / "new").glob("*.json")) if include_new else []

    bundle = DsaBundle(
        patterns=list(library),
        library=library,
        animations=animations,
        decision_tree=tree,
        problems=[],
        batches=[],
    )
    batches = [(p, _read_json(p)) for p in archived + new]
    archived_docs = {p.name: b for p, b in batches if p.parent == archived_dir}
    by_id: dict[int, Problem] = {}
    for path, batch in batches:
        name = f"{path.parent.name}/{path.name}"
        bundle.batches.append(name)
        # Like check-batch: ids and tag sets of the *archived* batches, other than this one.
        foreign_ids = {
            p.get("leetcodeId"): other
            for other, doc in archived_docs.items()
            if other != path.name or path.parent != archived_dir
            for p in doc.get("problems") or []
        }
        foreign_tags = {
            other: doc.get("algorithms") or []
            for other, doc in archived_docs.items()
            if (other != path.name or path.parent != archived_dir) and doc.get("algorithms")
        }
        errors, warnings, review, _, _ = check_batch(name, batch, foreign_ids, foreign_tags)
        bundle.errors += errors
        bundle.warnings += warnings
        bundle.review += review
        for tag in batch.get("algorithms") or []:
            if tag not in library:
                bundle.errors.append(f"{name}: tag {tag!r} has no library entry")
        for p in batch.get("problems") or []:
            if (
                p.get("descriptionStatus") != "original"
                or not str(p.get("description") or "").strip()
            ):
                bundle.skipped += 1
                continue
            lc = p.get("leetcodeId")
            if lc in by_id:
                bundle.errors.append(f"{name} {lc}: leetcodeId also in {by_id[lc].batch}")
                continue
            io = p.get("io") or {}
            try:
                by_id[lc] = Problem(
                    leetcode_id=int(lc),
                    title=str(p["title"]),
                    difficulty=str(p["difficulty"]),
                    url=str(p["url"]),
                    algorithm=str(p["algorithm"]),
                    acceptable=list(p.get("acceptableAlgorithms") or []),
                    time_weight=int(p["timeWeightMinutes"]),
                    description=str(p["description"]).strip(),
                    io_input=str(io.get("input") or "").strip(),
                    io_output=str(io.get("output") or "").strip(),
                    examples=[
                        {
                            "input": str(e.get("input") or ""),
                            "output": str(e.get("output") or ""),
                            "explanation": (
                                str(e["explanation"]) if e.get("explanation") else None
                            ),
                        }
                        for e in io.get("examples") or []
                    ],
                    near_duplicate_of=(
                        int(p["nearDuplicateOf"]) if p.get("nearDuplicateOf") is not None else None
                    ),
                    batch=name,
                )
            except (KeyError, TypeError, ValueError):
                continue  # already reported by check_batch (missing metadata)
    bundle.problems = sorted(by_id.values(), key=lambda pr: pr.leetcode_id)

    # Library and decision tree.
    if not library:
        bundle.errors.append("library: no entries")
    for tag, entry in library.items():
        for f in ("whenToReach", "tells", "pseudocode"):
            if not entry.get(f):
                bundle.errors.append(f"library {tag!r}: missing {f}")
    nodes = tree.get("nodes") or {}
    if not nodes or tree.get("root") not in nodes:
        bundle.errors.append("decision tree: missing nodes or root")
    for node_id, node in nodes.items():
        if node.get("kind") == "question":
            for branch in ("yes", "no"):
                if node.get(branch) not in nodes:
                    bundle.errors.append(f"decision tree {node_id}: {branch} → unknown node")
        elif node.get("kind") == "leaf":
            base = re.sub(r"\s*\(.*\)$", "", str(node.get("pattern", ""))).strip()
            if base not in library:
                bundle.warnings.append(f"decision tree {node_id}: pattern {base!r} is not a tag")
        else:
            bundle.errors.append(f"decision tree {node_id}: unknown kind {node.get('kind')!r}")
    for pr in bundle.problems:
        if pr.near_duplicate_of is not None and pr.near_duplicate_of not in by_id:
            bundle.warnings.append(
                f"{pr.batch} {pr.leetcode_id}: nearDuplicateOf {pr.near_duplicate_of} "
                "is not an imported problem"
            )
    return bundle
