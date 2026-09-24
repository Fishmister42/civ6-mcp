"""Turn-start aggregate read, and a board screenshot the agent can actually look at.

Both exist for the same reason: the agent was spending most of its per-turn call budget
re-reading state one tool at a time before it could decide anything. `get_game_summary`
collapses that into one call. `get_board_screenshot` gives it the thing a human has and a
text interface cannot reconstruct — spatial layout at a glance.

Principle I, stated plainly because the screenshot looks like the riskier of the two and is
actually the safer:

- The summary composes the SAME gated queries the individual tools use. It adds no new read
  path, so every visibility gate landed in spec-005 R2-R6 carries through automatically. If
  a query is admissible on its own it is admissible here, and if one is ever found leaking,
  fixing it fixes both.
- The screenshot is the player's own window. That is not merely *compatible* with
  human-parity, it is the definition of it — a person sees exactly these pixels. The hazard
  is not hidden game state, it is HARNESS state: a FireTuner window, a debug overlay or a
  console composited into frame. This project has a release-blocking finding about exactly
  that. So the capture is **gated fail-closed**: a frame that cannot be positively
  identified as the game's own in-game view is withheld rather than delivered.

Deliberately NOT included in the summary: anything static. The full tech and civic trees,
the building catalogue, terrain tables. The agent needs what changed, not a fresh copy of
the rulebook every turn.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from civ_mcp import narrate as nr

#: Text that must appear in an OCR of the frame for it to count as the game's own in-game
#: view. Measured on this host: tesseract reads the top bar and the world tracker reliably.
_IN_GAME_MARKERS = ("WORLD TRACKER", "CHOOSE RESEARCH", "MELEE STRENGTH", "MOVEMENT")
#: Text whose presence means we are NOT looking at live gameplay.
_NOT_IN_GAME = ("SINGLE PLAYER", "LOAD GAME", "JOINS THE", "MAIN MENU")
#: Harness/debug chrome. Any of these in frame withholds the capture outright.
_CONTAMINANTS = ("FIRETUNER", "FIRE TUNER", "TUNER", "LUA CONSOLE", "DEBUG MENU")


async def _safe(label: str, coro) -> tuple[str, Any]:
    """Run one sub-query; a failure degrades the section, never the whole summary."""
    try:
        return label, await asyncio.wait_for(coro, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return label, RuntimeError(f"{type(exc).__name__}: {exc}")


#: Per-section line caps for concise mode. Research & Civics and Government are 72% of the
#: full payload and the least dynamic part of it — the available-tech list barely moves
#: turn to turn — so they take the hard caps. TURN BLOCKERS is never capped: it is the one
#: section that must be acted on, and truncating it would defeat the point of hoisting it.
_CONCISE_CAPS = {
    "RESEARCH & CIVICS (current + available only)": 8,
    "GOVERNMENT": 8,
    "DIPLOMACY": 14,
    "CITIES & PRODUCTION": 16,
    "OUR UNITS": 16,
    "STATE": 12,
    "RESOURCES": 6,
    "VISIBLE FOREIGN UNITS": 12,
}


def _cap(title: str, body: str, concise: bool) -> str:
    if not concise:
        return body
    limit = _CONCISE_CAPS.get(title)
    if limit is None:
        return body
    lines = body.splitlines()
    if len(lines) <= limit:
        return body
    hidden = len(lines) - limit
    return "\n".join(lines[:limit]) + f"\n  ... +{hidden} more (call the specific tool for full detail)"


async def build_game_summary(gs, concise: bool = False) -> str:
    """One turn-start read covering everything dynamic a decision needs.

    Sections degrade independently: `get_game_overview` in particular is flaky on this host
    (it raises rather than retries when the tuner returns nothing), and a summary that
    collapsed entirely because one section failed would be worse than the individual tools
    it replaces.
    """
    # SEQUENTIAL, not asyncio.gather. The tuner accepts ONE connection: firing these nine
    # queries concurrently made them reset each other and every section came back
    # "ConnectionResetError". Fourth time this single-connection limit has broken something
    # in this codebase that looked like a logic bug. Concurrency buys nothing here anyway —
    # the connection is the bottleneck, not the client.
    results: dict[str, Any] = {}
    for label, factory in (
        ("overview", gs.get_game_overview),
        ("tech", gs.get_tech_civics),
        ("policies", gs.get_policies),
        ("cities", gs.get_cities),
        ("units", gs.get_units),
        ("resources", gs.get_empire_resources),
        ("diplomacy", gs.get_diplomacy),
        ("threats", gs.get_threat_scan),
        ("sessions", gs.get_diplomacy_sessions),
    ):
        k, v = await _safe(label, factory())
        results[k] = v

    out: list[str] = [
        "=== TURN SUMMARY (auto) ===" if concise else "=== TURN SUMMARY ==="
    ]

    def section(title: str, key: str, render) -> None:
        val = results.get(key)
        if isinstance(val, Exception):
            out.append(f"\n-- {title} --\n  (unavailable: {val})")
            return
        try:
            body = render(val)
        except Exception as exc:  # noqa: BLE001
            body = f"  (could not render: {type(exc).__name__}: {exc})"
        out.append(f"\n-- {title} --\n{_cap(title, body, concise)}")

    section("STATE", "overview", nr.narrate_overview)
    section("RESEARCH & CIVICS (current + available only)", "tech", nr.narrate_tech_civics)
    section("GOVERNMENT", "policies", nr.narrate_policies)
    section("CITIES & PRODUCTION", "cities", lambda v: nr.narrate_cities(*v))

    # WHAT EACH CITY CAN ACTUALLY BUILD — districts and buildings only.
    #
    # The root cause of "we have yet to build a single district". The cities section lists
    # what is already built and what needs a builder, but never what is BUILDABLE, so the
    # agent was choosing from memory — and unit names are the ones it reliably knows.
    # Measured over one block: five production choices, all units (Slinger, Slinger,
    # Warrior, Settler, Builder), and get_district_advisor never called once. It was not
    # ignoring districts; it could not see them.
    #
    # Units are deliberately omitted: it already builds those without help, and the point
    # is to surface the categories it is missing rather than to reprint everything.
    cities_res = results.get("cities")
    if not isinstance(cities_res, Exception) and cities_res:
        try:
            city_list = cities_res[0] if isinstance(cities_res, tuple) else cities_res
        except Exception:  # noqa: BLE001
            city_list = []
        build_lines: list[str] = []
        for c in list(city_list)[:4]:  # cap: each city is another tuner round-trip
            cid = getattr(c, "city_id", None)
            cname = getattr(c, "name", "?")
            if cid is None:
                continue
            try:
                opts = await asyncio.wait_for(gs.list_city_production(cid), timeout=45)
            except Exception as exc:  # noqa: BLE001
                build_lines.append(f"  {cname}: (unavailable: {type(exc).__name__})")
                continue
            districts = [o for o in opts if o.category == "DISTRICT" and not o.is_repair]
            buildings = [o for o in opts if o.category == "BUILDING" and not o.is_repair]
            if not districts and not buildings:
                build_lines.append(f"  {cname}: no districts or buildings available")
                continue
            build_lines.append(f"  {cname} (id {cid}):")
            if districts:
                build_lines.append(
                    "    DISTRICTS: "
                    + ", ".join(f"{o.item_name}({o.turns}t)" for o in districts[:8])
                )
            if buildings:
                build_lines.append(
                    "    BUILDINGS: "
                    + ", ".join(f"{o.item_name}({o.turns}t)" for o in buildings[:8])
                )
        if build_lines:
            out.append(
                "\n-- BUILDABLE NOW (districts & buildings) --\n"
                + "\n".join(build_lines)
                + "\n  Districts need a target tile: get_district_advisor(city_id) gives "
                "placement yields, then set_city_production(item_type='DISTRICT', "
                "target_x=, target_y=)."
            )
    section("OUR UNITS", "units", nr.narrate_units)

    # Settlers and Builders decide the early game and were getting lost in a long unit
    # list — the owner reported not always seeing a settler in the summary at all. Hoist
    # them out by name so they cannot be missed regardless of how the list is capped.
    units = results.get("units")
    if not isinstance(units, Exception) and units:
        key = []
        for u in units:
            t = (getattr(u, "unit_type", "") or "").upper()
            if "SETTLER" in t or "BUILDER" in t:
                key.append(
                    f"  {getattr(u, 'unit_type', '?')} at "
                    f"({getattr(u, 'x', '?')},{getattr(u, 'y', '?')}) "
                    f"id={getattr(u, 'unit_id', getattr(u, 'id', '?'))} "
                    f"moves={getattr(u, 'moves', '?')}"
                )
        out.append(
            "\n-- SETTLERS & BUILDERS (never truncated) --\n"
            + ("\n".join(key) if key else "  none — build a Settler if you want to expand")
        )
    section("RESOURCES", "resources", nr.narrate_empire_resources)
    section("DIPLOMACY", "diplomacy", nr.narrate_diplomacy)

    # Threats are visible foreign units near us. The underlying query is visibility-gated;
    # this adds no new read.
    threats = results.get("threats")
    if isinstance(threats, Exception):
        out.append(f"\n-- VISIBLE FOREIGN UNITS --\n  (unavailable: {threats})")
    elif not threats:
        out.append("\n-- VISIBLE FOREIGN UNITS --\n  none in sight")
    else:
        lines = [
            f"  {getattr(t, 'unit_type', '?')} of {getattr(t, 'owner_name', '?')} "
            f"at ({getattr(t, 'x', '?')},{getattr(t, 'y', '?')})"
            + (f" — {getattr(t, 'note', '')}" if getattr(t, "note", "") else "")
            for t in threats
        ]
        out.append("\n-- VISIBLE FOREIGN UNITS --\n" + "\n".join(lines))

    # Anything that BLOCKS end_turn belongs at the bottom, where it is read last and acted
    # on first. A diplomacy session left open is the single most common turn blocker, and a
    # block once burned 25 consecutive refused end_turn calls without the agent ever
    # looking for one.
    sessions = results.get("sessions")
    if isinstance(sessions, Exception):
        out.append(f"\n-- TURN BLOCKERS --\n  (unavailable: {sessions})")
    elif sessions:
        out.append(
            "\n-- TURN BLOCKERS --\n"
            + nr.narrate_diplomacy_sessions(sessions)
            + "\n  !! An open diplomacy session will REFUSE end_turn. "
            "Answer it with respond_to_diplomacy before ending the turn."
        )
    else:
        out.append("\n-- TURN BLOCKERS --\n  none detected")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Board screenshot
# ---------------------------------------------------------------------------


def _ocr(path: Path) -> str:
    try:
        return subprocess.run(
            ["tesseract", str(path), "-"], capture_output=True, text=True, timeout=60
        ).stdout.upper()
    except Exception:
        return ""


def capture_board(out_dir: Path) -> tuple[Path | None, str]:
    """Capture the game window. Returns (path, reason-if-withheld).

    Fail-closed. A frame is delivered only when it can be POSITIVELY identified as the
    game's own in-game view and shows no harness chrome. Everything else is withheld with a
    reason — including the case where the check itself could not run, because an
    unverifiable frame is exactly the one that should not reach a model.
    """
    try:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "audit"))
        from shot import shoot  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return None, f"capture unavailable: {type(exc).__name__}: {exc}"

    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / f"board_{int(time.time())}.png"
    try:
        shoot(str(raw))
    except BaseException as exc:  # noqa: BLE001
        return None, f"capture failed: {type(exc).__name__}: {exc}"

    # OCR the FULL-RESOLUTION frame — the contamination check must run on every pixel that
    # was captured, not on a downscale that could blur harness text out of legibility.
    text = _ocr(raw)

    # Only then downscale for delivery. A 2.7 MB PNG is ~3.6 MB of base64 per turn; at
    # 1280px JPEG it is roughly 20x smaller and the board is still perfectly readable,
    # which matters because this is meant to be affordable every turn rather than a treat.
    path = raw
    try:
        from PIL import Image as _PILImage

        im = _PILImage.open(raw).convert("RGB")
        if im.width > 1280:
            im = im.resize((1280, round(im.height * 1280 / im.width)), _PILImage.LANCZOS)
        small = raw.with_suffix(".jpg")
        im.save(small, "JPEG", quality=80, optimize=True)
        path = small
    except Exception:
        pass  # deliver the PNG rather than nothing
    if not text:
        return None, "withheld: frame could not be verified (OCR returned nothing)"
    for bad in _CONTAMINANTS:
        if bad in text:
            return None, f"withheld: harness UI in frame ({bad})"
    if any(m in text for m in _NOT_IN_GAME):
        return None, "withheld: not an in-game view (menu or loading screen)"
    if not (re.search(r"TURN\s*\d+\s*/\s*\d+", text) or any(m in text for m in _IN_GAME_MARKERS)):
        return None, "withheld: frame does not positively identify as the game board"
    return path, ""
