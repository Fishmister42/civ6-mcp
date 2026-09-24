#!/usr/bin/env python3
"""Window-scoped screenshot via mss (no X grab — unlike ImageMagick `import`).

CivSolver host-facts: `import -window` takes an X server grab and can freeze the
whole desktop if the target window vanishes mid-capture. mss does not grab.
"""
import subprocess, sys, time
from pathlib import Path

def civ_window_geometry():
    out = subprocess.run(["xdotool", "search", "--name", "^Civilization VI$"],
                         capture_output=True, text=True, timeout=10).stdout.split()
    if not out:
        raise SystemExit("no Civilization VI window found")
    wid = out[-1]
    g = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                       capture_output=True, text=True, timeout=10).stdout
    d = dict(l.split("=", 1) for l in g.strip().splitlines())
    return wid, int(d["X"]), int(d["Y"]), int(d["WIDTH"]), int(d["HEIGHT"])

def shoot(path):
    import mss
    from PIL import Image
    wid, x, y, w, h = civ_window_geometry()
    with mss.mss() as sct:
        raw = sct.grab({"left": x, "top": y, "width": w, "height": h})
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path, (w, h)

if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else f"/tmp/civ-{int(time.time())}.png"
    print(*shoot(p))
