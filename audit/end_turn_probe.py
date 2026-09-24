#!/usr/bin/env python3
"""Direct end_turn probe — no model, no cost.

Answers the one question three model-driven blocks left open: can the candidate
advance consecutive turns on this host at all? Drives end_turn through the MCP
server exactly as an agent would, and verifies each turn by reading the game's
own turn counter back (FR-011) rather than trusting the tool's reply.
"""
import asyncio, json, os, sys, time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/end_turn_probe")
N = int(sys.argv[2]) if len(sys.argv) > 2 else 10


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    log = (OUT / "end_turn_probe.jsonl").open("w")
    params = StdioServerParameters(
        command="uv", args=["run", "--directory", str(REPO), "civ-mcp"], env={**os.environ}
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()

            async def call(name, args=None):
                res = await asyncio.wait_for(s.call_tool(name, args or {}), timeout=600)
                return "\n".join(getattr(c, "text", "") or "" for c in res.content)

            async def turn_now():
                import re
                body = await call("run_lua", {
                    "code": 'print("TURNIS="..Game.GetCurrentGameTurn())\nprint("---END---")',
                    "context": "gamecore"})
                m = re.search(r"TURNIS=(\d+)", body)
                return int(m.group(1)) if m else None

            start = await turn_now()
            print(f"start turn: {start}")
            ok = 0
            prev = start
            for i in range(N):
                t0 = time.time()
                try:
                    body = await call("end_turn", {
                        "tactical": f"Probe turn {i+1}: no manual unit orders issued this turn.",
                        "strategic": "Audit probe for spec 005 — measuring whether consecutive "
                                     "turns can be advanced through the MCP surface at all.",
                        "tooling": "end_turn's five reflection fields are declared optional with "
                                   "empty defaults in the JSON Schema but rejected when empty at "
                                   "runtime; supplying them explicitly here.",
                        "planning": "Advance turns and read Game.GetCurrentGameTurn() back each time.",
                        "hypothesis": "The turn counter increments by one per successful call.",
                    })
                except Exception as exc:
                    body = f"EXC {type(exc).__name__}: {exc}"
                dur = time.time() - t0
                after = await turn_now()
                advanced = (after is not None and prev is not None and after > prev)
                if advanced:
                    ok += 1
                row = {"i": i + 1, "turn_before": prev, "turn_after": after,
                       "advanced": advanced, "seconds": round(dur, 1),
                       "reply_head": body[:300]}
                log.write(json.dumps(row) + "\n"); log.flush()
                print(f"[{i+1}/{N}] {prev} -> {after} advanced={advanced} {dur:.0f}s  {body[:110]!r}")
                if after is None:
                    print("  client unreadable — stopping"); break
                prev = after
            print(json.dumps({"start_turn": start, "end_turn": prev,
                              "consecutive_turns_advanced": ok,
                              "turns_requested": N}, indent=2))
            (OUT / "end_turn_summary.json").write_text(json.dumps(
                {"start_turn": start, "end_turn": prev,
                 "consecutive_turns_advanced": ok, "turns_requested": N}, indent=2))

asyncio.run(main())
