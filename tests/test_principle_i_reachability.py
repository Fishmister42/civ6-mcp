"""Principle I reachability guard — spec-005 V1.

R1-R6 fixed six places where the agent could see what a human player cannot. Six correct
patches leave the seventh to be written next month. This is the control that makes them
stay fixed: it fails when a NEW ungated read of another player appears in the Lua that
feeds the tool surface.

Two different kinds of check, deliberately:

`test_no_foreign_visibility_handles` is ABSOLUTE and has no allowlist. Taking
``PlayersVisibility[<someone else>]`` is never defensible — it is asking the engine what a
rival can see. R2 removed the only instance, which had been computing each rival's
map-exploration percentage, a number with no human route at all.

`test_ungated_player_loops_are_ratcheted` is a RATCHET, not a proof. A mechanical sweep for
"loop over players without a nearby gate" over-reports badly: when the spec-005 audit ran it
by hand, most hits were false positives — the gate was the enclosing block, or the loop only
read PlayerConfigurations for a name, or it ran only after the game was already over. So
this pins the count that has been reviewed and fails on additions. It does not assert the
pinned ones are clean; it asserts nobody added an eighth without looking.

When this test fails because you added a loop: read it. If the read is gated, or reads
nothing a human could not obtain, raise the module's number here and say why in the ledger
below. If it is not gated, gate it. Do not raise the number to make the test pass.
"""

from __future__ import annotations

import pathlib
import re

LUA_DIR = pathlib.Path(__file__).resolve().parent.parent / "src" / "civ_mcp" / "lua"

#: Indices that are the LOCAL player in these modules. Anything else indexing
#: PlayersVisibility is a Principle I violation by construction.
LOCAL_PLAYER_NAMES = {"me", "id"}

#: Reviewed count of "for i = 0, 6x" player loops that a mechanical gate-sweep flags as
#: ungated, per module, as of spec-005 Phase 1 completion (2026-09-24). Each was read by
#: hand during the audit; the reason is why the flag is a false positive or accepted.
REVIEWED_UNGATED: dict[str, tuple[int, str]] = {
    "diplomacy": (
        1,
        "open-session scan: emits only when DiplomacyManager.FindOpenSessionID(me, i) >= 0, "
        "i.e. we are literally in conversation with them",
    ),
    "economy": (
        1,
        "trade-destination PRIMARY path: filtered by UnitManager.CanStartOperation, the "
        "engine's own legality check — this is the game's trade-route picker. The fallback "
        "path was the leak and R4 gated it",
    ),
    "map": (
        4,
        "per-tile scans whose gate is the enclosing `if revealed`/`if visible` block, plus "
        "the static map dump and ownership delta used by operator capture, not by tools",
    ),
    "overview": (
        8,
        "game-over winner lookup (runs only once the game has ended), id->name maps built "
        "from PlayerConfigurations, and city-state envoy/suzerain reads that the game shows "
        "for met city-states",
    ),
    "religion": (
        4,
        "pantheon/belief availability filtering — reproduces the game's own picker, which "
        "hides taken beliefs without disclosing who took them",
    ),
    "units": (1, "per-tile foreign-unit enumeration gated by the enclosing `if visible`"),
    "victory": (
        1,
        "religion-count aggregation behind the Religious Victory screen, which the game "
        "shows to every player",
    ),
}

_GATE_TOKENS = ("HasMet", "IsRevealed", "IsVisible", "GetVisibilityOn", "vis >= ")
_PLAYER_LOOP = re.compile(r"for\s+i\s*=\s*0\s*,\s*6[0-9]")
#: How far below the loop header to look for a gate. The audit used 16 lines.
_WINDOW = 16


def _strip_comments(line: str) -> str:
    """Drop Python (#) and Lua (--) comment text.

    The first run of this guard failed on prose: an R2 comment that *describes* the removed
    ``PlayersVisibility[<rival>]`` read. A checker that cannot tell code from a comment about
    code reports the fix as the defect, and the natural response — deleting the explanation —
    would make the codebase worse to pass a test.
    """
    stripped = line.lstrip()
    if stripped.startswith("#") or stripped.startswith("--"):
        return ""
    for marker in ("  # ", "  -- "):
        i = line.find(marker)
        if i != -1:
            line = line[:i]
    return line


def _lua_modules() -> list[pathlib.Path]:
    return sorted(p for p in LUA_DIR.glob("*.py") if not p.name.startswith("_"))


def _ungated_loop_lines(path: pathlib.Path) -> list[int]:
    lines = [_strip_comments(l) for l in path.read_text().splitlines()]
    out = []
    for n, line in enumerate(lines):
        if not _PLAYER_LOOP.search(line):
            continue
        window = "\n".join(lines[n : n + _WINDOW])
        if not any(tok in window for tok in _GATE_TOKENS):
            out.append(n + 1)
    return out


def test_no_foreign_visibility_handles() -> None:
    """PlayersVisibility may only ever be indexed by the local player.

    No allowlist. Asking the engine what a RIVAL can see has no human equivalent under any
    diplomatic visibility level, so there is no threshold at which it becomes admissible.
    """
    offenders: list[str] = []
    for path in _lua_modules():
        for n, raw in enumerate(path.read_text().splitlines(), start=1):
            line = _strip_comments(raw)
            for m in re.finditer(r"PlayersVisibility\[([^\]]+)\]", line):
                idx = m.group(1).strip()
                if idx not in LOCAL_PLAYER_NAMES:
                    offenders.append(f"{path.name}:{n}  PlayersVisibility[{idx}]")
    assert not offenders, (
        "PlayersVisibility indexed by someone other than the local player "
        f"({sorted(LOCAL_PLAYER_NAMES)}):\n  " + "\n  ".join(offenders)
    )


def test_ungated_player_loops_are_ratcheted() -> None:
    """No NEW ungated player loop may appear without review.

    A rise here is not automatically a violation — the sweep over-reports. It means a loop
    was added that nobody has read against Principle I yet.
    """
    problems: list[str] = []
    for path in _lua_modules():
        found = _ungated_loop_lines(path)
        allowed, _reason = REVIEWED_UNGATED.get(path.stem, (0, ""))
        if len(found) > allowed:
            problems.append(
                f"{path.name}: {len(found)} ungated player loops, {allowed} reviewed. "
                f"Lines: {found}. Read the new one(s); gate it, or raise the count in "
                f"REVIEWED_UNGATED with a reason."
            )
    assert not problems, "\n".join(problems)


def test_ratchet_ledger_matches_reality() -> None:
    """The pinned counts must not drift ABOVE reality either.

    A stale-high number is a silently widened allowance: it would let a genuinely new
    ungated loop slip in under a budget earned by one that has since been fixed.
    """
    stale: list[str] = []
    for path in _lua_modules():
        allowed, _ = REVIEWED_UNGATED.get(path.stem, (0, ""))
        found = len(_ungated_loop_lines(path))
        if allowed > found:
            stale.append(
                f"{path.name}: ledger allows {allowed} but only {found} exist — "
                f"lower it to {found}."
            )
    assert not stale, "\n".join(stale)
