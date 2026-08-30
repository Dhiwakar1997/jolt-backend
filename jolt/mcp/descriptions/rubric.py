"""Versioned grading rubric — a product asset (LLD §4 descriptions/).

Agents grade free-text recall against the expected answer using this rubric and
stamp `rubric_version` on each grading so a later rubric change is auditable. Bump
RUBRIC_VERSION on any change to the text below and review the diff deliberately.
"""

RUBRIC_VERSION = "2026-08-30.1"

RUBRIC_TEXT = """\
Grade the learner's free-text recall of a concept against the expected answer,
then map your judgement to an FSRS rating (1=Again, 2=Hard, 3=Good, 4=Easy):

- 1 Again — the recall is absent, wrong, or contradicts the expected answer.
- 2 Hard  — the core idea is present but partial, hedged, or recovered with
            visible effort / omissions.
- 3 Good  — the expected answer is reproduced correctly with minor gaps.
- 4 Easy  — complete, precise, and fluent, including nuance beyond the minimum.

Grade the *substance* of the recall, not spelling or phrasing. Do not reward a
multiple-choice guess: the free-text (stage 1) is the graded artefact; the MCQ
(stage 2) only feeds the provisional grade. When unsure between two ratings,
choose the lower one — under-crediting costs a slightly earlier review, while
over-crediting silently erodes retention.
"""
