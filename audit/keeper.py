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
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SECRETS = Path("/home/matt/CivSolver/secrets.yaml")
SAVE_NAME = "KEEPER"  # short on purpose: long names truncate in the load list UI
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
    import socket

    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


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
    if not port_open():
        return {"state": "wedged_or_starting"}
    from civ_mcp.connection import GameConnection

    conn = GameConnection()
    try:
        await conn.connect()
        if conn.gamecore_index is None:
            return {"state": "menu"}
        lines = await conn.execute_read('print("T="..Game.GetCurrentGameTurn())')
        turn = None
        for ln in lines:
            if "T=" in ln:
                turn = int(ln.split("T=")[1].split()[0])
        return {"state": "in_game", "turn": turn}
    except Exception as exc:
        return {"state": "unreachable", "error": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        # The tuner accepts ONE connection. A leaked health check makes the very
        # next play block report "Cannot connect" against a perfectly healthy client.
        await _release(conn)


async def checkpoint() -> str | None:
    """Save through the operator tuner path. Returns the save name, or None."""
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
    try:
        import httpx
        import yaml

        key = os.environ.get("OPENROUTER_API_KEY") or yaml.safe_load(
            SECRETS.read_text()
        ).get("openrouter_api_key")
        d = httpx.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {key}"}, timeout=25,
        ).json()["data"]
        return float(d["total_credits"]) - float(d["total_usage"])
    except Exception:
        return None


async def _await_in_game(timeout: float, tick: float = 8.0) -> bool:
    """Async poll for an in-game state.

    This must NOT go through wait_for(): heal() already runs inside an event loop,
    and asyncio.run() from there raises "cannot be called from a running event loop"
    — which silently turned every reload into a reported failure.
    """
    end = time.time() + timeout
    while time.time() < end:
        st = await game_state()
        if st.get("state") == "in_game":
            return True
        await asyncio.sleep(tick)
    return False


async def heal(log) -> str:
    """Bring the client back to an in-game state. Returns what it did."""
    st = await game_state()
    if st["state"] == "in_game":
        return "already_in_game"

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
    elif st["state"] == "wedged_or_starting":
        # Port closed: either still starting up, or gone. Give it a chance first.
        if not wait_for(port_open, 60):
            log("port never opened - recycling")
            recycle()
            action = "recycled_no_port"
        else:
            action = "port_recovered"
    else:
        action = "at_menu"

    if not wait_for(port_open, 240):
        return action + "+tuner_never_opened"
    # The menu renders well after the tuner binds; give it room, then drive the load.
    time.sleep(35)
    log(f"loading save '{SAVE_NAME}' via direct clicks")
    click("single_player", "load_game")
    time.sleep(3)
    click("first_save")
    click("load_button", settle=6)
    time.sleep(35)
    click("continue", settle=8)

    ok = await _await_in_game(240, tick=8)
    return action + ("+reloaded" if ok else "+reload_failed")


def play_block(out: Path, turns: int, model: str, cycle: int) -> dict:
    d = out / f"cycle-{cycle:04d}"
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
    ap.add_argument("--model", default="qwen/qwen3-235b-a22b-2507")
    ap.add_argument("--max-cycles", type=int, default=0, help="0 = until stopped")
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

        killed = reap_stragglers()
        if killed:
            log(f"reaped stragglers holding the tuner: {killed}")
            time.sleep(3)

        healed = await heal(log)
        st = await game_state()
        if st.get("state") != "in_game":
            log(f"cannot reach an in-game state ({st}) - retrying next cycle")
            record(kind="cycle", cycle=cycle, healed=healed, state=st, played=None)
            time.sleep(30)
            continue

        saved = await checkpoint()
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
