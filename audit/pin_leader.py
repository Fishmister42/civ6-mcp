#!/usr/bin/env python3
"""Pin the human slot's leader/civ at the HostGame Lua state, then read it back.

The `CivSim DEFAULT` preset carries map/difficulty/speed but NOT a leader — every
slot reads `Random Leader`. CivSolver's verified fix is a direct write through
PlayerConfigurations[0] in the HostGame state (src/civsim_harness/run/preparation.py).
Verification is a read-back through the same table, never the Create Game UI.
"""
import asyncio, sys
from civ_mcp import tuner_client as tc

LEADER = sys.argv[1] if len(sys.argv) > 1 else "LEADER_CYRUS"
CIV = sys.argv[2] if len(sys.argv) > 2 else "CIVILIZATION_PERSIA"


async def main():
    reader, writer = await tc.connect()
    app, states = await tc.handshake(reader, writer)
    # States arrive as one space-separated "0 Main State 1 DebugHotloadCache ..." blob;
    # find HostGame's index by probing rather than parsing that shape.
    idx = None
    for i in range(0, 40):
        r = await tc.execute_lua(reader, writer, i,
                                 'print(PlayerConfigurations and "HAS_PC" or "NO_PC")', timeout=1.5)
        if r and "HAS_PC" in r:
            probe = await tc.execute_lua(reader, writer, i,
                'local ok,v = pcall(function() return PlayerConfigurations[0]:GetLeaderTypeName() end); '
                'print(ok and ("LEADER="..tostring(v)) or "ERR")', timeout=2.0)
            if probe and "LEADER=" in probe:
                idx = i
                print(f"state {i}: {probe.strip()[:120]}")
                break
    if idx is None:
        print("could not find a state exposing PlayerConfigurations[0]")
        return
    w = await tc.execute_lua(reader, writer, idx,
        'local pc = PlayerConfigurations[0]; '
        f'local okL = pcall(function() pc:SetLeaderTypeName("{LEADER}") end); '
        f'local okC = pcall(function() pc:SetCivilizationTypeName("{CIV}") end); '
        'print("set: leader="..tostring(okL).." civ="..tostring(okC))', timeout=5.0)
    print("write:", (w or "").strip()[:200])
    rb = await tc.execute_lua(reader, writer, idx,
        'local pc = PlayerConfigurations[0]; '
        'print("READBACK leader="..tostring(pc:GetLeaderTypeName())'
        '.." civ="..tostring(pc:GetCivilizationTypeName()))', timeout=5.0)
    print("readback:", (rb or "").strip()[:200])
    writer.close()

asyncio.run(main())
