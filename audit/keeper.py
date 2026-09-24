#!/usr/bin/env python3
"""Keeper — hold a live Civilization VI game in continuous play.

The agent loop that babysits this supervisor handles judgement (is the owner asking
for Steam back, is a new crash class appearing, is the budget spent). Everything
mechanical lives here, because the project's own rule is to guarantee a property by
structure rather than by an agent remembering to check it each tick.

Cycle:
    1. reap stragglers        - the tuner accepts ONE connection; a leftover agent
                                process makes a healthy client look unreachable
    2. health                  - process alive? port 4318 accepting? in a game?
    3. heal                    - relaunch / recycle / reload as the diagnosis requires
    4. checkpoint              - save through the operator tuner path (Network.SaveGame
                                works on this host; civ6-mcp's own comment says it does
                                not, and is wrong about that)
    5. play                    - one bounded block of minimal_agent.py
    6. record                  - one JSONL line per cycle, then repeat

Operator capability is deliberately NOT agent capability: the keeper reaches the tuner
directly for saving and health, the agent only ever sees the MCP tool surface.

    uv run python audit/keeper.py --out <dir> [--turns-per-block 12] [--max-cycles 0]

Stop: create the file <out>/STOP, or the OpenRouter budget floor is hit, or --max-cycles.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SECRETS = Path("/home/matt/CivSolver/secrets.yaml")
SAVE_NAME = "KEEPER"  # short on purpose: long names truncate in the load list UI
SAVE_DIR = (
    Path.home() / ".local/share/aspyr-media/Sid Meier's Civilization VI/Saves/Single"
)
STEAM_APPID = "289070"

# Menu coordinates, 1920x1200 window at 0,0. Measured 2026-09-24. Their OCR menu
# navigation works but landed 1 of 3 here (truncated save names; wide OCR boxes
# causing edge-clicks), so the keeper clicks known positions instead.
CLICK = {
    "single_player": (863, 488),
    "load_game": (1077, 660),
    "first_save": (628, 298),
    "load_button": (640, 1149),
    "continue": (573, 1131),
    # Create Game path, measured 2026-09-24 while building the first Cyrus seed by hand.
    "create_game": (1089, 833),
    "advanced_setup": (959, 1088),
    "load_configuration": (330, 1159),
    "preset_first": (620, 217),
    "load_config_button": (640, 1148),
    "leader_dropdown": (715, 240),
    "start_game": (1485, 1164),
}


def sh(cmd: list[str], timeout: int = 45) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def civ_pid() -> int | None:
    """pgrep -x only. NEVER pgrep -f: it matches this very command line."""
    out = sh(["pgrep", "-x", "Civ6"]).split()
    return int(out[0]) if out else None


def reap_stragglers() -> list[int]:
    """Kill leftover agent/probe processes holding the single tuner connection."""
    killed = []
    out = sh(["ps", "-eo", "pid,comm,args"])
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, comm, args = parts[0], parts[1], parts[2]
        if not comm.startswith("python"):
            continue
        if ("minimal_agent" in args or "end_turn_probe" in args) and pid.isdigit():
            if int(pid) != os.getpid():
                try:
                    os.kill(int(pid), 15)
                    killed.append(int(pid))
                except ProcessLookupError:
                    pass
    return killed


def port_open(host: str = "127.0.0.1", port: int = 4318) -> bool:
    """Bare TCP probe. USE SPARINGLY — see below.

    Measured 2026-09-24: this opens a socket and closes it WITHOUT completing the
    FireTuner handshake. heal() used to call it through wait_for() up to ~80 times per
    load, and upstream's README states the tuner hangs after a bad handshake and does
    not recover without a process recycle. That is the wedge the keeper kept
    diagnosing: it was manufacturing it, one aborted handshake at a time, while the
    game itself stayed perfectly playable (confirmed at TURN 16/250 with a raw
    handshake failing).

    Liveness is now judged from the SCREEN, which costs the tuner nothing.
    """
    import socket

    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


#: Leader the keeper is minding. A cycle that finds a different leader in progress
#: reloads rather than playing and checkpointing someone else's game.
EXPECT_LEADER = os.environ.get("KEEPER_EXPECT_LEADER", "LEADER_CYRUS")

#: Per-run prefix for cycle directories. Without it a restart reuses cycle-0001 and
#: Recorder's append mode silently merges two different games into one record.
RUN_TAG = time.strftime("run-%Y%m%d-%H%M%S")


async def _release(conn) -> None:
    """Hand the single tuner slot back, whatever the client exposes to do it with."""
    for name in ("disconnect", "close"):
        fn = getattr(conn, name, None)
        if fn is None:
            continue
        try:
            r = fn()
            if asyncio.iscoroutine(r):
                await r
            return
        except Exception:
            pass
    try:
        w = getattr(conn, "writer", None)
        if w is not None:
            w.close()
    except Exception:
        pass


async def game_state() -> dict:
    """down | menu | in_game, plus the turn when in game."""
    if civ_pid() is None:
        return {"state": "down"}
    from civ_mcp.connection import GameConnection

    conn = GameConnection()
    try:
        await conn.connect()
        if conn.gamecore_index is None:
            return {"state": "menu"}
        lines = await conn.execute_read(
            'local me = Game.GetLocalPlayer(); '
            'print("T="..Game.GetCurrentGameTurn()); '
            'print("LEADER="..tostring(PlayerConfigurations[me]:GetLeaderTypeName()))'
        )
        turn, leader = None, None
        for ln in lines:
            if "T=" in ln:
                try:
                    turn = int(ln.split("T=")[1].split()[0])
                except ValueError:
                    pass
            if "LEADER=" in ln:
                leader = ln.split("LEADER=")[1].strip()
        return {"state": "in_game", "turn": turn, "leader": leader}
    except Exception as exc:
        return {"state": "unreachable", "error": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        # The tuner accepts ONE connection. A leaked health check makes the very
        # next play block report "Cannot connect" against a perfectly healthy client.
        await _release(conn)


async def checkpoint(attempts: int = 3, leader: str | None = None) -> str | None:
    # Never write the checkpoint from a game that is not ours — that is how the intended
    # save got overwritten. `leader` comes from the caller's own game_state() read, on the
    # connection it already opened: re-reading it here needed a second connection, the
    # tuner allows one, and it returned None nearly every cycle.
    if EXPECT_LEADER and leader != EXPECT_LEADER:
        return None

    """Save through the operator tuner path. Returns the save name, or None.

    Retries: cycle 1 on 2026-09-24 logged checkpoint=None while the very next call
    played 12 turns fine, so a single attempt lands too close behind the connection
    _await_in_game just released. A failed checkpoint is not cosmetic — it leaves the
    keeper reloading a stale save after a crash, silently discarding the turns since.
    """
    for attempt in range(attempts):
        got = await _checkpoint_once()
        if got:
            return got
        await asyncio.sleep(4)
    return None


async def _checkpoint_once() -> str | None:
    from civ_mcp.connection import GameConnection

    conn = GameConnection()
    try:
        await conn.connect()
        if conn.gamecore_index is None:
            return None
        await conn.execute_write(
            "local f = {}; "
            f'f.Name = "{SAVE_NAME}"; f.Location = SaveLocations.LOCAL_STORAGE; '
            "f.Type = SaveTypes.SINGLE_PLAYER; f.IsAutosave = false; f.IsQuicksave = false; "
            "local ok = Network.SaveGame(f); "
            'print("SAVED="..tostring(ok))'
        )
        return SAVE_NAME
    except Exception:
        return None
    finally:
        await _release(conn)


def click(*names: str, settle: float = 2.5) -> None:
    wid = sh(["xdotool", "search", "--name", "^Civilization VI$"]).split()
    if not wid:
        return
    w = wid[-1]
    try:
        subprocess.run(["xdotool", "windowactivate", w], capture_output=True, timeout=45)
    except Exception:
        pass
    time.sleep(1)
    for n in names:
        x, y = CLICK[n]
        # Measured 2026-09-24: xdotool blocks well past 15 s while the client is
        # loading and the X server is busy. A TimeoutExpired here aborted the whole
        # heal, so the keeper reported reload_failed on a client that was fine.
        try:
            subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"],
                           capture_output=True, timeout=45)
        except Exception:
            pass
        time.sleep(settle)


def screen_text() -> str:
    """OCR of the game window. Empty string if the window is gone or capture fails.

    The keeper navigates on SCREEN state, not on tuner state. Measured 2026-09-24:
    polling game_state() every 8 s through a load opened ~30 tuner handshakes during
    a state transition and wedged the tuner, which put the keeper in a
    load -> wedge -> recycle loop of its own making. Upstream's README says the same
    thing from the other side: the tuner hangs after a bad handshake and does not
    recover without a process recycle. So: look at the screen, and touch the tuner
    once, at the end, to confirm.
    """
    try:
        sys.path.insert(0, str(REPO / "audit"))
        from shot import shoot as _shoot  # type: ignore

        tmp = "/tmp/civsim_keeper_screen.png"
        _shoot(tmp)
        return sh(["tesseract", tmp, "-"], timeout=45)
    except BaseException:
        return ""


def main_menu_visible() -> bool:
    return "Single Player" in screen_text()


# The splash's own button is UNREADABLE to OCR. Measured 2026-09-24: tesseract
# returns nothing at all below y~1050 on this screen — the "CONTINUE GAME" banner is
# stylised text on a ribbon — so a detector looking for that string is False forever,
# and the keeper sat on a splash it could not see through four separate "fixes".
# Detect the splash by the body copy, which reads fine, and dismiss it by acting
# rather than by aiming at a button whose position moves with the panel width.
SPLASH_MARKERS = ("JOINS THE", "A UNIQUE LAND UNIT", "FEATURES & ABILITIES")


def continue_splash_visible() -> bool:
    up = screen_text().upper()
    return any(m in up for m in SPLASH_MARKERS)


def dismiss_splash() -> bool:
    """Clear the post-load civ splash. Returns True if the screen changed.

    Tries keyboard first (no coordinates to get wrong), then a short row of candidate
    click positions along the button's band. The button sits under the left text panel
    whose width varies by civ, so a single fixed x that worked for one leader missed
    for another.
    """
    wid = sh(["xdotool", "search", "--name", "^Civilization VI$"]).split()
    if not wid:
        return False
    w = wid[-1]
    for key in ("Return", "space", "Escape"):
        try:
            subprocess.run(["xdotool", "key", "--window", w, key],
                           capture_output=True, timeout=45)
        except Exception:
            pass
        time.sleep(4)
        if not continue_splash_visible():
            return True
    try:
        subprocess.run(["xdotool", "windowactivate", w], capture_output=True, timeout=45)
    except Exception:
        pass
    for x in (478, 495, 573, 640):
        try:
            subprocess.run(["xdotool", "mousemove", str(x), "1158", "click", "1"],
                           capture_output=True, timeout=45)
        except Exception:
            pass
        time.sleep(5)
        if not continue_splash_visible():
            return True
    return False


IN_GAME_MARKERS = ("WORLD TRACKER", "CHOOSE RESEARCH", "MELEE STRENGTH", "MOVEMENT")


def _looks_in_game(up: str) -> bool:
    import re as _re

    if _re.search(r"TURN\s*\d+\s*/\s*\d+", up):
        return True
    return any(m in up for m in IN_GAME_MARKERS)


def in_game_visible() -> bool:
    up = screen_text().upper()
    if "SINGLE PLAYER" in up or any(m in up for m in SPLASH_MARKERS):
        return False
    return _looks_in_game(up)


def new_game_same_config(log) -> bool:
    """Start a FRESH game from the CivSim DEFAULT preset with Cyrus in the human slot.

    Same configuration, new map seed — the owner's experiment design: run to a turn
    ceiling, then reseed rather than play one game forever, so results are comparable
    across runs instead of being one long anecdote.

    Drives the Create Game UI because the preset deliberately does NOT pin a leader (every
    slot reads "Random Leader"), and the Lua write at the setup state could not be reached
    on this host. The leader list is alphabetical and scrolled by a measured amount; the
    caller VERIFIES identity afterwards rather than trusting these clicks.
    """
    if not wait_for(main_menu_visible, 300, tick=6):
        log("reseed: no main menu")
        return False
    log("reseed: Create Game -> Advanced Setup -> load CivSim DEFAULT")
    click("single_player"); time.sleep(2)
    click("create_game", settle=5)
    click("advanced_setup", settle=4)
    click("load_configuration", settle=4)
    click("preset_first", settle=3)
    click("load_config_button", settle=7)

    log("reseed: selecting Cyrus in the human slot")
    click("leader_dropdown", settle=3)
    wid = sh(["xdotool", "search", "--name", "^Civilization VI$"]).split()
    if wid:
        w = wid[-1]
        try:
            subprocess.run(["xdotool", "windowactivate", w], capture_output=True, timeout=30)
            subprocess.run(["xdotool", "mousemove", "390", "600"], capture_output=True, timeout=30)
            for _ in range(6):  # past Cyrus
                subprocess.run(["xdotool", "click", "5"], capture_output=True, timeout=20)
                time.sleep(0.3)
            for _ in range(3):  # back up onto him
                subprocess.run(["xdotool", "click", "4"], capture_output=True, timeout=20)
                time.sleep(0.3)
            time.sleep(2)
            subprocess.run(["xdotool", "mousemove", "335", "331", "click", "1"],
                           capture_output=True, timeout=30)
        except Exception as exc:  # noqa: BLE001
            log(f"reseed: leader selection failed ({exc})")
            return False
    time.sleep(3)
    log("reseed: Start Game")
    click("start_game", settle=10)
    return True


def launch() -> None:
    subprocess.Popen(
        ["setsid", "steam", f"steam://rungameid/{STEAM_APPID}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


def recycle() -> None:
    pid = civ_pid()
    if pid:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        for _ in range(12):
            time.sleep(1)
            if civ_pid() is None:
                break
        else:
            pid = civ_pid()
            if pid:
                os.kill(pid, 9)
                time.sleep(4)
    launch()


def wait_for(pred, timeout: float, tick: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(tick)
    return False


def segfault_count() -> int:
    out = sh(["sudo", "-n", "dmesg"], timeout=20)
    return sum(1 for ln in out.splitlines() if "Civ6" in ln and "segfault" in ln)


def budget_remaining() -> float | None:
    """Headroom on THIS KEY, not the account.

    Corrected 2026-09-24 after the owner caught it. This used /api/v1/credits and returned
    total_credits - total_usage, which is an ACCOUNT-WIDE figure covering every key ever
    issued: it read $2.25 while this key had $31.86 left of its $80 limit. The keeper was
    therefore minutes from halting itself on a budget it was not spending. /api/v1/key
    reports limit_remaining for the key actually in use, which is the only number that
    governs whether the next request is allowed.
    """
    try:
        import httpx
        import yaml

        key = os.environ.get("OPENROUTER_API_KEY") or yaml.safe_load(
            SECRETS.read_text()
        ).get("openrouter_api_key")
        d = httpx.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {key}"}, timeout=25,
        ).json()["data"]
        rem = d.get("limit_remaining")
        if rem is not None:
            return float(rem)
        # A key with no limit set is bounded only by the account; treat as ample rather
        # than as zero, because returning 0 here would stop the keeper on a null field.
        return float("inf")
    except Exception:
        return None


async def _await_in_game(timeout: float, tick: float = 10.0) -> bool:
    """Drive whatever is on screen towards an in-game state, then confirm ONCE.

    Deliberately screen-first. An earlier version polled game_state() every 8 s, which
    is ~30 tuner handshakes across a load, and that is what kept wedging the tuner. One
    connection at the end is enough to confirm what the screen already says.

    It also DISMISSES the post-load civ splash whenever it turns up, rather than in a
    separate one-shot window beforehand. Measured 2026-09-24: a slow load put the splash
    on screen after that window had closed, so the keeper sat watching a splash it knew
    was not in-game for five minutes without ever clicking the button in front of it.
    A wait that can see a blocker should be able to clear it.

    Not via wait_for(): heal() runs inside an event loop and asyncio.run() from there
    raises "cannot be called from a running event loop".
    """
    end = time.time() + timeout
    dismissed = 0
    while time.time() < end:
        txt = screen_text()
        up = txt.upper()
        if any(m in up for m in SPLASH_MARKERS):
            dismissed += 1
            dismiss_splash()
            await asyncio.sleep(tick)
            continue
        if "SINGLE PLAYER" not in up and _looks_in_game(up):
            st = await game_state()
            if st.get("state") == "in_game":
                return True
        await asyncio.sleep(tick)
    return False


async def heal(log) -> str:
    """Bring the client back to an in-game state. Returns what it did."""
    st = await game_state()
    if st["state"] == "in_game":
        # Which game? Measured 2026-09-24: the keeper was restarted while a DIFFERENT
        # save was loaded, saw "in_game", played it, and checkpointed it straight over
        # the intended save — destroying 37 turns of the run it exists to protect
        # (recovered from an autosave). "A game is running" was never the question;
        # "is this OUR game" is.
        who = st.get("leader")
        if not EXPECT_LEADER:
            return "already_in_game"
        if who == EXPECT_LEADER:
            return "already_in_game"
        # FAIL CLOSED. who is None when the identity read fails, and the first version
        # treated that as "carry on" — which is how the wrong game got played and
        # checkpointed a second time, minutes after the first. A guard that protects
        # against destroying the run must not pass when it cannot tell.
        log(f"in game as {who!r}, expected {EXPECT_LEADER} - reloading ours")
        recycle()

    # A cycle can begin on the post-load civ splash: the save is loaded, GameCore is
    # not resolvable yet, so game_state() reads "menu" — and waiting for the MAIN menu
    # from there waits for a screen that will never come. Clear the splash instead.
    if continue_splash_visible():
        log("cycle began on the continue splash; dismissing rather than seeking a menu")
        if await _await_in_game(300, tick=10):
            return "splash_dismissed"

    if st["state"] == "down":
        log("client down - launching")
        launch()
        action = "launched"
    elif st["state"] == "unreachable":
        # THE WEDGE SIGNATURE. The wedged tuner still ACCEPTS the TCP connection and
        # then resets it on handshake, so port_open() reads healthy throughout and a
        # port-wait never fires the recycle. Measured 2026-09-24: repeated
        # ConnectionResetError with Civ6 alive, port 4318 accepting, and the game
        # visibly in progress on screen. Only a process recycle clears it.
        log(f"tuner accepts then resets - wedged; recycling ({st.get('error')})")
        recycle()
        action = "recycled_wedge"
    else:
        action = "at_menu"

    # Wait for the menu on SCREEN. Never poll the port: each bare probe is an aborted
    # handshake, and enough of them wedge the tuner we are trying to bring back.
    if not wait_for(main_menu_visible, 300, tick=6):
        log("main menu never appeared - will retry next cycle")
        return action + "+no_main_menu"
    log(f"main menu up; loading save '{SAVE_NAME}' via direct clicks")
    click("single_player", "load_game")
    time.sleep(3)
    click("first_save")
    click("load_button", settle=6)

    # No separate splash window: _await_in_game dismisses the splash whenever it
    # appears, so a slow load cannot land it outside a fixed watching period.
    ok = await _await_in_game(420, tick=10)
    return action + ("+reloaded" if ok else "+reload_failed")


def play_block(out: Path, turns: int, model: str, cycle: int) -> dict:
    d = out / f"{RUN_TAG}-cycle-{cycle:04d}"
    d.mkdir(parents=True, exist_ok=True)
    cmd = [
        "uv", "run", "--directory", str(REPO), "python", str(REPO / "audit/minimal_agent.py"),
        "--turns", str(turns), "--max-steps", "600", "--max-seconds", "3000",
        "--shot-interval", "120", "--calls-per-turn", "7",
        "--model", model, "--ban", "send_diplomatic_action",
        "--out", str(d),
    ]
    with (d / "driver.log").open("w") as lg:
        rc = subprocess.run(cmd, stdout=lg, stderr=subprocess.STDOUT, timeout=3600).returncode
    summ = {}
    sp = d / "summary.json"
    if sp.exists():
        try:
            summ = json.loads(sp.read_text())
        except Exception:
            pass
    return {"returncode": rc, "dir": str(d), **summ}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--turns-per-block", type=int, default=12)
    ap.add_argument("--model", default="z-ai/glm-5.3-flash")
    ap.add_argument("--max-cycles", type=int, default=0, help="0 = until stopped")
    ap.add_argument("--reseed-at", type=int, default=0,
                    help="when the game reaches this turn, archive it and start a fresh "
                         "seed on the same configuration (0 = never)")
    ap.add_argument("--budget-floor", type=float, default=0.25,
                    help="stop when OpenRouter credit falls below this")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stop = out / "STOP"
    jl = (out / "keeper.jsonl").open("a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)

    def record(**row) -> None:
        row["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        jl.write(json.dumps(row) + "\n")
        jl.flush()

    segs = segfault_count()
    cycle = 0
    log(f"keeper up - baseline Civ6 segfaults in dmesg: {segs}")

    while True:
        cycle += 1
        if stop.exists():
            log("STOP file present - standing down")
            record(kind="stop", reason="stop_file", cycle=cycle)
            break
        if args.max_cycles and cycle > args.max_cycles:
            record(kind="stop", reason="max_cycles", cycle=cycle)
            break

        bud = budget_remaining()
        if bud is not None and bud < args.budget_floor:
            log(f"budget floor reached (${bud:.2f}) - standing down")
            record(kind="stop", reason="budget", remaining=bud, cycle=cycle)
            break

        log(f"cycle {cycle}: checking for stragglers")
        killed = reap_stragglers()
        if killed:
            log(f"reaped stragglers holding the tuner: {killed}")
            time.sleep(3)

        # Every phase is bounded AND announced. Measured three times today: a keeper that
        # is stuck and a keeper that is working look identical from outside, because the
        # only log lines were "cycle started" and "cycle done". Silence was the failure
        # mode and silence was never instrumented — the same shape as 25 refused end_turn
        # calls looking healthy because calls were flowing.
        log(f"cycle {cycle}: health check")
        try:
            healed = await asyncio.wait_for(heal(log), timeout=900)
        except asyncio.TimeoutError:
            log(f"cycle {cycle}: heal exceeded 900s - recycling and retrying next cycle")
            record(kind="cycle", cycle=cycle, healed="heal_timeout", played=None)
            recycle()
            continue
        log(f"cycle {cycle}: healed={healed}; reading state")
        try:
            st = await asyncio.wait_for(game_state(), timeout=120)
        except asyncio.TimeoutError:
            log(f"cycle {cycle}: game_state timed out - retrying next cycle")
            record(kind="cycle", cycle=cycle, healed=healed, state="timeout", played=None)
            time.sleep(15)
            continue
        if st.get("state") != "in_game":
            log(f"cannot reach an in-game state ({st}) - retrying next cycle")
            record(kind="cycle", cycle=cycle, healed=healed, state=st, played=None)
            time.sleep(30)
            continue

        # Turn ceiling: reseed rather than play one game indefinitely.
        turn_now = st.get("turn") or 0
        if args.reseed_at and turn_now >= args.reseed_at:
            log(f"cycle {cycle}: turn {turn_now} >= reseed ceiling {args.reseed_at} - "
                f"archiving and starting a fresh seed on the same configuration")
            archive = SAVE_DIR / f"CYRUS-seed-t{turn_now}-{time.strftime('%H%M%S')}.Civ6Save"
            try:
                shutil.copy2(SAVE_DIR / f"{SAVE_NAME}.Civ6Save", archive)
                log(f"cycle {cycle}: archived finished game to {archive.name}")
            except Exception as exc:  # noqa: BLE001
                log(f"cycle {cycle}: archive failed ({exc}) - reseeding anyway")
            recycle()
            if new_game_same_config(log) and await _await_in_game(600, tick=10):
                fresh = await game_state()
                log(f"cycle {cycle}: reseeded -> turn {fresh.get('turn')} "
                    f"as {fresh.get('leader')}")
                record(kind="reseed", cycle=cycle, from_turn=turn_now,
                       new_turn=fresh.get("turn"), leader=fresh.get("leader"),
                       archived=archive.name)
                if fresh.get("leader") != EXPECT_LEADER:
                    log(f"cycle {cycle}: reseed produced {fresh.get('leader')} - "
                        "NOT reseeding again this run, reloading instead")
                    args.reseed_at = 0
            else:
                log(f"cycle {cycle}: reseed failed - retrying next cycle")
                record(kind="reseed_failed", cycle=cycle, from_turn=turn_now)
            continue

        log(f"cycle {cycle}: checkpointing")
        try:
            saved = await asyncio.wait_for(
                checkpoint(leader=st.get("leader")), timeout=180
            )
        except asyncio.TimeoutError:
            log(f"cycle {cycle}: checkpoint timed out - playing without a fresh save")
            saved = None
        log(f"cycle {cycle}: turn {st.get('turn')} - checkpoint={saved} - playing "
            f"{args.turns_per_block} turns")

        played = play_block(out, args.turns_per_block, args.model, cycle)

        now_segs = segfault_count()
        new_crash = now_segs > segs
        segs = now_segs
        after = await game_state()

        record(
            kind="cycle", cycle=cycle, healed=healed,
            turn_before=st.get("turn"), turn_after=after.get("turn"),
            checkpoint=saved, new_segfault=new_crash,
            budget=bud, played=played,
        )
        log(f"cycle {cycle} done: turns_ended={played.get('turns_ended')} "
            f"turn {st.get('turn')} -> {after.get('turn')} new_segfault={new_crash}")

    jl.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
