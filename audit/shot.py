#!/usr/bin/env python3
"""Capture the Civilization VI window.

Two corrections learned the hard way on 2026-09-24:

1. `mss` grabs a SCREEN REGION, not a window. It is not "window-scoped" — anything
   stacked above the game is captured instead of it. A whole keeper cycle's frames
   came back showing a terminal and a browser with the game nowhere in them. `xwd`
   has the same problem here: with no backing store it returns the composited
   pixels. So the window has to be RAISED before the grab.

2. `xdotool getwindowgeometry` reports coordinates relative to the frame parent
   under a reparenting WM. `xwininfo`'s "Absolute upper-left" is the real position,
   and the two disagreed by 33px vertically on this host while the game was
   windowed at 1024x768.

Still never ImageMagick `import`: it takes an X server grab and, if the target
disappears mid-capture, freezes the entire desktop (measured: 80 minutes).
"""
import re
import subprocess
import sys
import time
from pathlib import Path


def _run(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


def civ_window_id() -> str:
    out = _run(["xdotool", "search", "--name", "^Civilization VI$"]).split()
    if not out:
        raise SystemExit("no Civilization VI window found")
    return out[-1]


def civ_window_geometry(wid: str | None = None):
    """(wid, x, y, w, h) in ABSOLUTE screen coordinates."""
    wid = wid or civ_window_id()
    info = _run(["xwininfo", "-id", wid])
    def grab(pat, default=None):
        m = re.search(pat, info)
        if m:
            return int(m.group(1))
        if default is not None:
            return default
        raise SystemExit(f"xwininfo missing {pat}")
    x = grab(r"Absolute upper-left X:\s+(-?\d+)")
    y = grab(r"Absolute upper-left Y:\s+(-?\d+)")
    w = grab(r"Width:\s+(\d+)")
    h = grab(r"Height:\s+(\d+)")
    return wid, x, y, w, h


def raise_window(wid: str) -> None:
    """Put the game on top so the grab actually sees it.

    `xdotool windowraise` EXITS 0 AND DOES NOTHING under this Cinnamon session, which
    is why the first attempt at this fix changed no pixels: the capture kept returning
    whatever was stacked above the game. windowactivate does restack. It takes keyboard
    focus, which is the price of a correct frame — and the keeper already activates this
    window to click it, so nothing new is lost.
    """
    for cmd in (["xdotool", "windowactivate", wid],
                ["wmctrl", "-i", "-a", wid]):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=20)
            if r.returncode == 0:
                return
        except Exception:
            continue


def shoot(path, raise_first: bool = True):
    import mss
    from PIL import Image

    wid, x, y, w, h = civ_window_geometry()
    if raise_first:
        raise_window(wid)
        time.sleep(0.6)
        # re-read: raising can change the geometry if the WM restacks or unmaximises
        wid, x, y, w, h = civ_window_geometry(wid)
    with mss.mss() as sct:
        raw = sct.grab({"left": x, "top": y, "width": w, "height": h})
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path, (w, h)


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else f"/tmp/civ-{int(time.time())}.png"
    print(*shoot(p))
