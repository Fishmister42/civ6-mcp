"""Regression suite for the audit harness's verdict classifier — spec-005.

This classifier decides whether a tool call COUNTED. Every coverage number in the audit
rests on it, so a bug here is a bug in the evidence, not just in a log line. It has been
wrong twice and both failures were silent:

1. Everything was "ok" because the MCP server narrates game-level refusals in the result
   body without setting isError, so twelve consecutive "Cannot connect to Civ 6" replies
   were recorded as successes after the client had crashed.
2. Substring matching over the whole body classified get_game_summary — 12 KB of entirely
   correct game state — as engine_refused, because "cannot" appears somewhere inside it and
   "Error:" matches "RuntimeError:" in a degraded section. Not cosmetic: the harness's stall
   detector counts non-applied verdicts, so a summary called every turn and always misread
   would eventually abort a block over a tool that was working.

Hence the split the cases below pin: structured markers (ERR:, FAILED:, WARN:SILENT_FAILURE)
match anywhere because they are signals; English phrasing only counts when it OPENS the
reply, because prose recurs inside any long narration.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "audit"))

from minimal_agent import classify  # noqa: E402

_SUMMARY = (
    pathlib.Path("/home/matt/CivSolver/specs/005-mcp-harness-pivot/evidence")
    / "remediations" / "game_summary_sample.txt"
)


@pytest.mark.parametrize(
    "body,expected",
    [
        # Transport: the client is gone. Must never read as a successful call.
        ("Cannot connect to Civ 6 at 127.0.0.1:4318. Is the game running?", "transport_failure"),
        # The server's own error shapes.
        ("Error: Empty overview response", "engine_refused"),
        ("Error executing tool get_diplomacy: 0 bytes read on a total of 8", "engine_refused"),
        # Structured signals, anywhere in the body.
        ("POLICIES_SET|Policies updated.\nWARN:SILENT_FAILURE - engine rejected slot 0",
         "engine_refused"),
        ("ERR:CANNOT_FOUND|Too close to another city", "engine_refused"),
        # end_turn's runtime rejection, which cost three blocks before it was understood.
        ("Empty reflections: tactical, strategic, tooling, planning, hypothesis.",
         "engine_refused"),
        # Plain English refusal opening the reply.
        ("Unit cannot found cities (not a settler or no moves)", "engine_refused"),
        # Successes, including ones whose text contains scary words later on.
        ("FOUNDED|11,23", "applied"),
        ("Turn 103 -> 104 | Score: 437 / Resources: HORSES 45/45", "applied"),
        ("CAPTURE_MOVE|47,14|from:46,21|now_at:46,21|BLOCKED (path may be blocked)",
         "applied"),
    ],
)
def test_classify(body: str, expected: str) -> None:
    assert classify(body) == expected


@pytest.mark.skipif(not _SUMMARY.exists(), reason="captured summary sample not present")
def test_long_summary_is_not_a_refusal() -> None:
    """A 12 KB correct summary must not be a refusal because prose appears inside it.

    Kept separate and named for what it protects: this is the exact regression that made a
    working tool look broken, and it is the case a future edit to the marker lists is most
    likely to reintroduce.
    """
    assert classify(_SUMMARY.read_text()) == "applied"
