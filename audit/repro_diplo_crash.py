#!/usr/bin/env python3
"""V3 — attempt a controlled reproduction of the send_diplomatic_action crash.

On 2026-09-24 a `send_diplomatic_action(DECLARE_FRIENDSHIP)` call was followed 1.6 s later
by a client segfault resolving at offset +0x0 to
`GameCore::Diplomacy::Action::Instance::GetType()`, reading a member at +8 off a null
`this`. The audit recorded that as ATTRIBUTED, not reproduced, and spec-005 V3 owes the
controlled attempt.

Design notes that matter for the result being worth anything:

- A non-reproduction is a complete result. Three clean attempts is a finding; it is not a
  failure to find one, and it must not be reported as "works fine" either. The honest
  statement is "did not reproduce in N attempts under these conditions".
- The kernel ring buffer is the oracle, not the tool's reply. The tool returned
  `ACCEPTED|...` immediately before the original crash, so its own answer says nothing.
- Each attempt records the dmesg segfault count before and after, and resolves any new
  address to a symbol the same way the audit did (nm -DC, ip - base).

Usage:  uv run python audit/repro_diplo_crash.py <out dir> [attempts]
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/v3")
ATTEMPTS = int(sys.argv[2]) if len(sys.argv) > 2 else 3
LIB_NAME = "libGameCore_XP2.so"


def dmesg_segfaults() -> list[str]:
    try:
        out = subprocess.run(
            ["sudo", "-n", "dmesg"], capture_output=True, text=True, timeout=30
        ).stdout
    except Exception:
        return []
    return [l for l in out.splitlines() if "Civ6" in l and "segfault" in l]


def resolve_symbol(line: str) -> str | None:
    """ip - library base -> nearest preceding symbol, the audit's own method."""
    m = re.search(r"ip\s+([0-9a-fx]+).*?in\s+(\S+)\[([0-9a-f]+)\+", line)
    if not m:
        return None
    ip = int(m.group(1), 16)
    lib, base = m.group(2), int(m.group(3), 16)
    if LIB_NAME not in lib:
        return f"(fault in {lib}, not {LIB_NAME}; ip-base = 0x{ip - base:x})"
    root = Path.home() / ".steam/debian-installation/steamapps/common/Sid Meier's Civilization VI"
    libs = list(root.rglob(LIB_NAME))
    if not libs:
        return None
    off = ip - base
    try:
        nm = subprocess.run(
            ["nm", "-DC", "--defined-only", str(libs[0])],
            capture_output=True, text=True, timeout=120,
        ).stdout
    except Exception:
        return None
    best = None
    for ln in nm.splitlines():
        parts = ln.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            addr = int(parts[0], 16)
        except ValueError:
            continue
        if addr <= off and (best is None or addr > best[0]):
            best = (addr, parts[2])
    if not best:
        return None
    return f"{best[1]}  (+0x{off - best[0]:x} from symbol start, offset 0x{off:x})"


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    log = (OUT / "v3_repro.jsonl").open("a", encoding="utf-8")

    def rec(**row):
        row["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log.write(json.dumps(row) + "\n")
        log.flush()
        print(json.dumps(row)[:400], flush=True)

    params = StdioServerParameters(
        command="uv", args=["run", "--directory", str(REPO), "civ-mcp"], env={**os.environ}
    )

    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()

            async def call(name, args=None, timeout=180):
                res = await asyncio.wait_for(s.call_tool(name, args or {}), timeout=timeout)
                return "\n".join(getattr(c, "text", "") or "" for c in res.content)

            diplo = await call("get_diplomacy")
            (OUT / "v3_diplomacy_state.txt").write_text(diplo)
            # "[player N]" appears only on civs we have met; unmet rows say "not met".
            targets = [
                int(m) for m in re.findall(r"\[player (\d+)\]", diplo)
            ]
            rec(kind="setup", met_players=targets, diplo_head=diplo[:300])
            if not targets:
                rec(kind="abort", reason="no met civilizations - cannot issue a diplomatic action")
                print("\nRESULT: NOT ATTEMPTED — no met civilizations in this save.")
                return 0

            target = targets[0]
            reproduced = False
            for i in range(1, ATTEMPTS + 1):
                before = dmesg_segfaults()
                t0 = time.time()
                try:
                    body = await call(
                        "send_diplomatic_action",
                        {"other_player_id": target, "action": "DECLARE_FRIENDSHIP"},
                    )
                except Exception as exc:
                    body = f"EXC {type(exc).__name__}: {exc}"
                dur = time.time() - t0
                # The original crash landed 1.6 s after the call returned.
                await asyncio.sleep(20)
                after = dmesg_segfaults()
                new = after[len(before):]
                sym = resolve_symbol(new[-1]) if new else None
                rec(
                    kind="attempt", n=i, target=target, seconds=round(dur, 2),
                    reply=body[:300], new_segfaults=new, symbol=sym,
                )
                if new:
                    reproduced = True
                    print(f"\nREPRODUCED on attempt {i}: {sym}")
                    break

            summary = {
                "attempts": i,
                "reproduced": reproduced,
                "target_player": target,
                "symbol": sym if reproduced else None,
            }
            (OUT / "v3_summary.json").write_text(json.dumps(summary, indent=2))
            print("\n" + json.dumps(summary, indent=2))
            if not reproduced:
                print(
                    f"\nRESULT: did NOT reproduce in {i} attempts. That is a complete result, "
                    "not a failure to find one — and it is not 'works fine' either."
                )
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
