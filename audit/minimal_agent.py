#!/usr/bin/env python3
"""Minimal MCP-only agent harness for the CivSolver spec-005 audit.

Deliberately thin. The agent's ONLY game-facing capability is the civ6-mcp
server's tool surface, reached over stdio JSON-RPC (FR-005). There is no
CivSolver harness code in this process and none on the import path.

What it records (FR-007, FR-008): every tool call with arguments, the outcome,
the latency, and the reason for every failure — nothing is discarded.

Model access goes through OpenRouter (constitution Principle VII). The key is
read in-process from the CivSolver secrets file and never printed.

Usage:
  uv run python audit/minimal_agent.py --turns 10 --out <dir> [--model SLUG]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent
SECRETS = Path("/home/matt/CivSolver/secrets.yaml")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """You are playing a game of Sid Meier's Civilization VI through a tool interface.

You are Cyrus of Persia. This is a BRAND NEW game: turn 1, Ancient Era, Pangaea, Small map,
Emperor difficulty, Online speed, 6 AI rivals, 9 city-states. You start with a Settler and a
Warrior and nothing else. Nothing on this map has been explored.

Your job this session is to PLAY — visibly and continuously. Breadth of action matters
more than optimal play: the point is to exercise the interface across as much of the
game as you can reach.

Loop, every turn:
  1. get_game_overview to orient.
  2. Look at what is actually actionable — units with moves, cities without production,
     idle research, pending diplomacy, notifications.
  3. ACT. Early game specifically: found your first city with the Settler (use
     get_settle_advisor if you want a site), send the Warrior out to explore, set city
     production and set research. Later: more settlers, improvements, diplomacy.
     Prefer trying a NEW kind of action you have not used yet over repeating one
     that already worked.
  4. end_turn when there is nothing useful left to do this turn.

ENDING TURNS IS THE POINT. A turn you never end is a turn that did not happen. You have a
budget of tool calls per turn; when you are told the budget is spent, call end_turn
IMMEDIATELY with no other tool call first.

Rules:
- Never call the same tool with the same arguments twice in a row. If something fails,
  read the error and do something different.
- Do not call run_lua. It is out of scope for this session.
- Do not call kill_game, launch_game, restart_and_load, load_save, load_game_save, or
  load_save_from_menu. The game is already loaded.
- Keep your reasoning brief. Spend your budget on tool calls, not prose.
- If a turn is done, end it. Do not stall.
- If end_turn is REFUSED, something is blocking the turn and calling it again will not help.
  Read the refusal: it names the blocker. A diplomacy prompt needs get_pending_diplomacy and
  then respond_to_diplomacy. Units awaiting orders need skip_remaining_units. Never call the
  same tool a third time after two identical refusals.
"""

BANNED = {
    "run_lua",
    "kill_game",
    "launch_game",
    "restart_and_load",
    "load_save",
    "load_game_save",
    "load_save_from_menu",
    "list_saves",
}


def load_openrouter_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    import yaml

    data = yaml.safe_load(SECRETS.read_text()) or {}
    key = data.get("openrouter_api_key")
    if not key:
        raise SystemExit("no OpenRouter key in env or secrets.yaml")
    return str(key)


@dataclass
class Recorder:
    out: Path
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        self.jsonl = (self.out / "tool_calls.jsonl").open("a", encoding="utf-8")

    def record(self, **row: Any) -> None:
        row["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.calls.append(row)
        self.jsonl.write(json.dumps(row) + "\n")
        self.jsonl.flush()

    def close(self) -> None:
        self.jsonl.close()


def shoot(out: Path, tag: str) -> str | None:
    """Window-scoped capture via mss. No X grab (see CivSolver host-facts)."""
    try:
        sys.path.insert(0, str(REPO / "audit"))
        from shot import shoot as _shoot  # type: ignore

        p = out / "shots" / f"{tag}.png"
        _shoot(str(p))
        return str(p)
    except BaseException as exc:  # noqa: BLE001 — capture is evidence, never a run-stopper
        # SystemExit is a BaseException, and shot.py raises it when the window is
        # gone (i.e. the client died). Catching only Exception killed a whole run.
        print(f"[shot] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


# An MCP call that returns without error is NOT proof the game action applied.
# The server narrates refusals in the result body and does not set isError, so a
# naive tally of "calls that returned" overcounts applied actions — the exact
# failure this project has already shipped once. Classify the body too.
TRANSPORT_MARKERS = ("Cannot connect to Civ 6", "ConnectionError", "Connection refused")
REFUSAL_MARKERS = (
    "WARN:SILENT_FAILURE",
    "ERR:",
    "FAILED:",
    "Error:",
    "not found",
    "No file currently selected",
    "cannot ",
    "Cannot ",
    "could not ",
    "Could not ",
    "couldn't",
    "not available",
    "not possible",
    "invalid",
    "Invalid",
)


def classify(body: str) -> str:
    """transport_failure | engine_refused | applied — from the narration itself."""
    if any(m in body for m in TRANSPORT_MARKERS):
        return "transport_failure"
    if any(m in body for m in REFUSAL_MARKERS):
        return "engine_refused"
    return "applied"


def mcp_tools_to_openai(tools: list[Any], extra_banned: set[str]) -> list[dict[str, Any]]:
    out = []
    for t in tools:
        if t.name in BANNED or t.name in extra_banned:
            continue
        schema = t.inputSchema or {"type": "object", "properties": {}}
        # AUDIT REPAIR (spec 005 finding): end_turn declares all five reflection
        # fields as optional strings with "default": "" and ships no `required`
        # array, while the runtime rejects them when empty. A model reading the
        # schema therefore believes it may call end_turn bare — and across three
        # blocks, no model ever ended a turn. Re-declare them as required so the
        # experiment tests the game interface rather than the schema bug.
        if t.name == "end_turn":
            schema = json.loads(json.dumps(schema))
            for f in schema.get("properties", {}).values():
                f.pop("default", None)
            schema["required"] = [
                "tactical", "strategic", "tooling", "planning", "hypothesis",
            ]
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": (t.description or "")[:1024],
                    "parameters": schema,
                },
            }
        )
    return out


async def chat(client: httpx.AsyncClient, key: str, model: str, messages, tools):
    r = await client.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://github.com/Fishmister42/CivSim-taskify",
            "X-Title": "CivSim spec-005 MCP audit",
        },
        json={
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "max_tokens": 1500,
        },
        timeout=180.0,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:600]}")
    return r.json()


async def run(args) -> int:
    out = Path(args.out)
    rec = Recorder(out)
    key = load_openrouter_key()

    params = StdioServerParameters(
        command="uv",
        args=["run", "--directory", str(REPO), "civ-mcp"],
        env={**os.environ},
    )

    started = time.time()
    turns_ended = 0
    last_shot = 0.0
    shots = 0
    transport_failures = 0
    calls_this_turn = 0
    # Repeat detector. Measured 2026-09-24: a block burned 25 consecutive end_turn calls,
    # every one engine_refused on the same Babylon diplomacy blocker, ~6 s apart, while the
    # agent narrated "diplomacy deadlock continues" each time. From outside the cycle looked
    # healthy — calls were flowing and nothing errored. This repo's git log opens with the
    # same shape: "a loop that succeeds ran 158 times unchecked".
    last_signature: tuple[str, str] | None = None
    repeat_count = 0

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = mcp_tools_to_openai(listed.tools, set(args.ban))
            (out / "tool_surface.json").write_text(
                json.dumps(
                    [
                        {"name": t.name, "description": t.description, "schema": t.inputSchema}
                        for t in listed.tools
                    ],
                    indent=2,
                )
            )
            print(f"[mcp] {len(listed.tools)} tools advertised, {len(tools)} exposed to the model")

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Play {args.turns} turns from turn 1. Start with "
                        "get_game_overview, then get your first city founded. "
                        "Briefly say WHY before each action — your reasoning is being "
                        "recorded as a paper trail alongside screenshots."
                    ),
                },
            ]

            async with httpx.AsyncClient() as http:
                for step in range(args.max_steps):
                    if turns_ended >= args.turns:
                        print(f"[done] {turns_ended} turns ended")
                        break
                    if time.time() - started > args.max_seconds:
                        print(f"[done] wall-clock budget spent after {turns_ended} turns")
                        break

                    if time.time() - last_shot > args.shot_interval:
                        p = shoot(out, f"t{turns_ended:03d}_s{step:03d}")
                        if p:
                            shots += 1
                            last_shot = time.time()

                    try:
                        resp = await chat(http, key, args.model, messages, tools)
                    except Exception as exc:
                        print(f"[model] {exc}", file=sys.stderr)
                        rec.record(kind="model_error", error=str(exc)[:500])
                        break

                    choice = resp["choices"][0]
                    msg = choice["message"]
                    messages.append(msg)

                    text = (msg.get("content") or "").strip()
                    if text:
                        print(f"[{step}] {text[:300]}")
                    # The agent's own prose is evidence too — the paper trail needs the
                    # reasoning that produced each command, not only the command.
                    rec.record(
                        kind="reasoning",
                        step=step,
                        turns_ended=turns_ended,
                        text=text,
                        tool_calls=[
                            {"name": tc["function"]["name"],
                             "arguments": tc["function"]["arguments"]}
                            for tc in (msg.get("tool_calls") or [])
                        ],
                        usage=resp.get("usage", {}),
                    )

                    tcs = msg.get("tool_calls") or []
                    if not tcs:
                        messages.append(
                            {
                                "role": "user",
                                "content": "Keep playing. Make a tool call.",
                            }
                        )
                        continue

                    for tc in tcs:
                        name = tc["function"]["name"]
                        try:
                            raw_args = json.loads(tc["function"]["arguments"] or "{}")
                        except json.JSONDecodeError:
                            raw_args = {}

                        t0 = time.time()
                        ok = True
                        err = None
                        try:
                            res = await asyncio.wait_for(
                                session.call_tool(name, raw_args), timeout=args.tool_timeout
                            )
                            parts = []
                            for c in res.content:
                                parts.append(getattr(c, "text", "") or f"<{type(c).__name__}>")
                            body = "\n".join(parts)
                            if getattr(res, "isError", False):
                                ok = False
                                err = body[:400]
                        except Exception as exc:
                            ok = False
                            err = f"{type(exc).__name__}: {exc}"[:400]
                            body = err
                        dur = time.time() - t0

                        verdict = "mcp_error" if not ok else classify(body)
                        rec.record(
                            kind="tool_call",
                            step=step,
                            tool=name,
                            args=raw_args,
                            ok=ok,
                            verdict=verdict,
                            error=err,
                            duration_s=round(dur, 2),
                            result_head=body[:600],
                            result_len=len(body),
                            turns_ended=turns_ended,
                        )
                        print(
                            f"[{step}] {verdict:17s} {name}({json.dumps(raw_args)[:100]}) {dur:.1f}s"
                        )
                        if verdict != "applied":
                            print(f"        -> {(err or body)[:200]}")
                        signature = (name, verdict)
                        if signature == last_signature:
                            repeat_count += 1
                        else:
                            last_signature, repeat_count = signature, 1

                        if verdict != "applied" and repeat_count == 3:
                            messages.append({
                                "role": "user",
                                "content": (
                                    f"STOP. You have called {name} {repeat_count} times in a row "
                                    f"and it was refused every time. Repeating it will not work. "
                                    "The refusal text names the blocker — read it. If a diplomacy "
                                    "prompt is blocking the turn, call get_pending_diplomacy and "
                                    "then respond_to_diplomacy to clear it. If a unit needs orders, "
                                    "use skip_remaining_units. Do something DIFFERENT."
                                ),
                            })
                        if verdict != "applied" and repeat_count >= 8:
                            print(f"[abort] {name} refused {repeat_count} times running — stopping")
                            rec.record(kind="abort", reason="repeat_refusal",
                                       tool=name, count=repeat_count, step=step)
                            shoot(out, f"stalled_{step:03d}")
                            return finish(args, rec, started, turns_ended, shots)

                        if verdict == "transport_failure":
                            transport_failures += 1
                            if transport_failures >= 3:
                                print("[abort] client unreachable three times — stopping")
                                shoot(out, f"transport_failure_{step:03d}")
                                rec.record(kind="abort", reason="client_unreachable", step=step)
                                return finish(args, rec, started, turns_ended, shots)
                        else:
                            transport_failures = 0

                        calls_this_turn += 1
                        if name == "end_turn" and verdict == "applied":
                            turns_ended += 1
                            calls_this_turn = 0
                            print(f"[turn] {turns_ended}/{args.turns} ended")
                            shoot(out, f"endturn_{turns_ended:03d}")
                            shots += 1
                            last_shot = time.time()

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": body[: args.result_cap],
                            }
                        )

                    if calls_this_turn >= args.calls_per_turn:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Your tool-call budget for this turn is spent "
                                    f"({calls_this_turn} calls). Call end_turn NOW. "
                                    "Do not call any other tool first."
                                ),
                            }
                        )

                    if len(messages) > args.history_cap:
                        messages = messages[:2] + messages[-(args.history_cap - 2) :]

    return finish(args, rec, started, turns_ended, shots)


def finish(args, rec: Recorder, started: float, turns_ended: int, shots: int) -> int:
    calls = [c for c in rec.calls if c.get("kind") == "tool_call"]
    applied = [c for c in calls if c.get("verdict") == "applied"]
    # WRITE tools only — a read that returned is not a game action applied, and the
    # project's own baseline of 11 counts applied ACTIONS, not successful queries.
    write_applied = sorted({c["tool"] for c in applied if not c["tool"].startswith("get_")})
    summary = {
        "model": args.model,
        "turns_ended": turns_ended,
        "wall_seconds": round(time.time() - started, 1),
        "tool_calls": len(calls),
        "by_verdict": {
            v: len([c for c in calls if c.get("verdict") == v])
            for v in ("applied", "engine_refused", "transport_failure", "mcp_error")
        },
        "distinct_tools_called": sorted({c["tool"] for c in calls}),
        "distinct_tools_applied": sorted({c["tool"] for c in applied}),
        "distinct_ACTION_tools_applied": write_applied,
        "distinct_ACTION_tools_applied_count": len(write_applied),
        "screenshots": shots,
    }
    (Path(args.out) / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    rec.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=10)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="anthropic/claude-sonnet-4.5")
    ap.add_argument("--max-steps", type=int, default=400)
    ap.add_argument("--max-seconds", type=float, default=3000)
    ap.add_argument("--tool-timeout", type=float, default=240)
    ap.add_argument("--shot-interval", type=float, default=120)
    ap.add_argument("--result-cap", type=int, default=6000)
    ap.add_argument("--history-cap", type=int, default=40)
    ap.add_argument("--calls-per-turn", type=int, default=10,
                    help="tool calls allowed per turn before the model is told to end it")
    ap.add_argument("--ban", nargs="*", default=[],
                    help="extra tool names withheld from the model "
                         "(used to isolate a crashing tool from a play block)")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
