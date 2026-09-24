#!/usr/bin/env python3
"""Build the Cyrus-run paper trail page from the harness's own records.

Input  : <run dir>/tool_calls.jsonl  (reasoning rows + tool_call rows)
         <run dir>/shots/*.png       (window-scoped captures)
Output : a single self-contained .html file with screenshots inlined as JPEG data URIs.

Nothing here invents content: every line of reasoning, every command and every
outcome is read straight out of the run record.
"""
from __future__ import annotations

import base64
import html
import io
import json
import re
import sys
from pathlib import Path

# Usage: build_papertrail.py <out.html> <maxw> <run dir> [<run dir> ...]
# Several run dirs are stitched into ONE continuous story, because the keeper
# resumes the same Cyrus game cycle after cycle and the artifact should read as
# one experiment rather than a pile of separate runs.
OUT = Path(sys.argv[1])
MAXW = int(sys.argv[2])
RUNS = [Path(a) for a in sys.argv[3:]]
RUN = RUNS[0]


def load_rows():
    """All rows across all runs, with a global turn index so turns keep counting up."""
    rows, base = [], 0
    for rd in RUNS:
        f = rd / "tool_calls.jsonl"
        if not f.exists():
            continue
        local = []
        for line in f.open():
            line = line.strip()
            if line:
                r = json.loads(line)
                r["_run"] = rd.name
                r["turns_ended"] = r.get("turns_ended", 0) + base
                local.append(r)
        rows.extend(local)
        ended = [
            r for r in local
            if r["kind"] == "tool_call" and r["tool"] == "end_turn"
            and r.get("verdict") == "applied"
        ]
        base += len(ended)
    return rows


def shot_data_uri(p: Path, maxw: int = MAXW, quality: int = 72) -> str | None:
    try:
        from PIL import Image

        im = Image.open(p).convert("RGB")
        if im.width > maxw:
            im = im.resize((maxw, round(im.height * maxw / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        print(f"  [shot] {p.name}: {exc}", file=sys.stderr)
        return None


def collect_shots() -> dict[int, list[tuple[int, str, str]]]:
    """turn index -> [(order, label, data uri)], across every run, offset the same way."""
    out: dict[int, list[tuple[str, str]]] = {}
    base = 0
    for rd in RUNS:
        _collect_one(rd, out, base)
        f = rd / "tool_calls.jsonl"
        if f.exists():
            base += sum(
                1 for l in f.open()
                if l.strip() and (json.loads(l).get("tool") == "end_turn"
                                  and json.loads(l).get("verdict") == "applied")
            )
    return out


def _collect_one(rd: Path, out: dict, base: int) -> None:  # noqa: C901
    d = rd / "shots"
    if not d.is_dir():
        return
    for p in sorted(d.glob("*.png")):
        m = re.match(r"endturn_(\d+)", p.stem)
        if m:
            # endturn_NNN is captured right AFTER turns_ended becomes NNN, so it belongs
            # to the section for the turn that just ENDED — key NNN-1, since sections are
            # keyed by turns_ended BEFORE the row and headed "Turn key+1". Filing it at
            # NNN put every end-of-turn frame one section late, so each turn appeared to
            # open with the previous turn's result.
            n = int(m.group(1)) + base
            turn = n - 1
            label = f"end of turn {n}"
            order = 1
        else:
            m2 = re.match(r"t(\d+)_s(\d+)", p.stem)
            turn = (int(m2.group(1)) if m2 else 0) + base
            step = int(m2.group(2)) if m2 else 0
            label = f"during turn {turn + 1}"
            order = 0
        uri = shot_data_uri(p)
        if uri:
            out.setdefault(turn, []).append((order, label, uri))


REFLECTION_FIELDS = ("tactical", "strategic", "tooling", "planning", "hypothesis")

VERDICT_LABEL = {
    "applied": "applied",
    "engine_refused": "refused by engine",
    "transport_failure": "client unreachable",
    "mcp_error": "tool error",
}


def fmt_args(a: dict) -> str:
    if not a:
        return "()"
    parts = []
    for k, v in a.items():
        s = str(v)
        if len(s) > 150:
            s = s[:150] + "…"
        parts.append(f"{k}={s}")
    return "(" + ", ".join(parts) + ")"


def main() -> None:
    rows = load_rows()
    shots = collect_shots()

    # Group steps by the turn they belong to (turns_ended is the count BEFORE this row).
    turns: dict[int, list[dict]] = {}
    for r in rows:
        turns.setdefault(r.get("turns_ended", 0), []).append(r)

    calls = [r for r in rows if r["kind"] == "tool_call"]
    applied = [r for r in calls if r.get("verdict") == "applied"]
    ended = [r for r in calls if r["tool"] == "end_turn" and r.get("verdict") == "applied"]
    action_tools = sorted({r["tool"] for r in applied if not r["tool"].startswith("get_")})
    all_tools = sorted({r["tool"] for r in calls})

    # The GAME's turn is not the same as the number of turns the agent played. A reload
    # rewinds the game to the last checkpoint while the record keeps every turn that was
    # played, so after a few restarts the two diverge — the page said "85 turns" while the
    # game sat at 76. Showing only the first is quietly misleading, so show both.
    game_turns: list[int] = []
    for c in calls:
        if c["tool"] != "end_turn" or c.get("verdict") != "applied":
            continue
        m = re.search(r"Turn\s+(\d+)\s*->\s*(\d+)", c.get("result_head") or "")
        if m:
            game_turns.append(int(m.group(2)))
    game_turn_now = max(game_turns) if game_turns else None
    game_turn_span = (min(game_turns), max(game_turns)) if game_turns else None

    summary = {}
    sp = RUN / "summary.json"
    if sp.exists():
        summary = json.loads(sp.read_text())
    model = summary.get("model", "qwen/qwen3-235b-a22b-2507")

    P: list[str] = []
    A = P.append

    A("<title>Cyrus Through a Tool Port</title>")
    A(STYLE)
    A('<div class="wrap">')

    # ---- header -------------------------------------------------------------
    A('<header class="masthead">')
    A('<p class="eyebrow">CivSim · spec 005 · MCP harness audit</p>')
    A("<h1>Cyrus Through a Tool Port</h1>")
    A(
        '<p class="dek">A language model plays Civilization VI from turn one with no screen '
        "and no mouse — only the 76 tool calls a Model Context Protocol server exposes. "
        "Every word of reasoning, every command and every reply below is lifted verbatim "
        "from the run record; the screenshots are the game window at the moment they were issued.</p>"
    )
    A('<dl class="meta">')
    for k, v in [
        ("Leader", "Cyrus · Persia"),
        ("Setup", "Gathering Storm · Emperor · Online · Pangaea Small · 6 AI · no turn timer"),
        ("Model", model),
        ("Interface", "civ6-mcp @ dd20190 over FireTuner :4318"),
    ]:
        A(f"<div><dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd></div>")
    A("</dl>")
    A("</header>")

    # ---- stats --------------------------------------------------------------
    note = (
        "Newest turn first \u2014 scroll down for earlier turns. Within a turn, steps run "
        "in the order the agent issued them."
    )
    if game_turn_span and game_turn_span[0] != game_turn_span[1]:
        note += (
            f" Section numbers count TURNS THE AGENT PLAYED; the game itself reached turn "
            f"{game_turn_span[1]}. They differ because a reload rewinds the game to its "
            f"last checkpoint while the record keeps every turn that was played."
        )
    A(f'<p class="order-note">{note}</p>')
    A('<section class="stats" aria-label="Run totals">')
    for n, label in [
        (game_turn_now if game_turn_now is not None else "—", "game turn reached"),
        (len(ended), "agent turns played"),
        (len(calls), "tool calls"),
        (len(applied), "applied"),
        (len(all_tools), "distinct tools"),
        (len(action_tools), "action tools"),
    ]:
        A(f'<div class="stat"><span class="num">{n}</span><span class="lbl">{label}</span></div>')
    A("</section>")

    # ---- turns --------------------------------------------------------------
    # Newest turn first. The page is a running log of an experiment still in progress, so
    # the thing a reader wants is the latest state, not turn 1 again. Steps WITHIN a turn
    # stay chronological — a turn only reads correctly forwards.
    for t in sorted(turns, reverse=True):
        steps = turns[t]
        if not any(s["kind"] == "tool_call" for s in steps):
            continue
        A('<section class="turn">')
        A(f'<h2><span class="tn">Turn {t + 1}</span></h2>')

        # "during" frames first, then the end-of-turn frame; cap at 4 rather than 2 so a
        # turn with several interval captures does not silently lose them.
        for _o, sh_label, uri in sorted(shots.get(t, []))[:4]:
            A('<figure class="shot">')
            A(f'<img src="{uri}" alt="Civilization VI window, {html.escape(sh_label)}">')
            A(f"<figcaption>{html.escape(sh_label)}</figcaption>")
            A("</figure>")

        for s in steps:
            if s["kind"] == "reasoning":
                txt = (s.get("text") or "").strip()
                if txt:
                    A(f'<blockquote class="think">{html.escape(txt)}</blockquote>')
            elif s["kind"] == "tool_call":
                v = s.get("verdict") or ("applied" if s.get("ok") else "mcp_error")
                args = s.get("args") or {}
                # end_turn carries the agent's own turn-level reasoning in five fields.
                # That prose is the paper trail, so give it its own block rather than
                # crushing it into an argument list.
                refl = None
                if s["tool"] == "end_turn" and any(
                    args.get(k) for k in REFLECTION_FIELDS
                ):
                    refl = {k: args.get(k, "") for k in REFLECTION_FIELDS}
                    args = {}
                if refl:
                    A('<div class="reflect">')
                    A('<p class="rlabel">The agent\u2019s own account of this turn</p>')
                    A("<dl>")
                    for k in REFLECTION_FIELDS:
                        val = (refl.get(k) or "").strip()
                        if val:
                            A(
                                f"<div><dt>{html.escape(k)}</dt>"
                                f"<dd>{html.escape(val)}</dd></div>"
                            )
                    A("</dl>")
                    A("</div>")
                A(f'<div class="step {html.escape(v)}">')
                A(
                    f'<code class="cmd">{html.escape(s["tool"])}'
                    f'<span class="args">{html.escape(fmt_args(args))}</span></code>'
                )
                A(
                    f'<span class="chip">{html.escape(VERDICT_LABEL.get(v, v))}'
                    f'<span class="dur">{s.get("duration_s", 0)}s</span></span>'
                )
                head = (s.get("result_head") or "").strip()
                if head:
                    A(f'<pre class="out">{html.escape(head[:700])}</pre>')
                A("</div>")
        A("</section>")

    A(
        '<footer><p>Generated from <code>tool_calls.jsonl</code> and the run\'s window captures. '
        "Verdicts are classified from each reply’s own text, because the server returns "
        "success for game-level refusals — a call that returned is not an action that applied.</p></footer>"
    )
    A("</div>")

    OUT.write_text("\n".join(P), encoding="utf-8")
    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT} ({kb:.0f} KB) — {len(ended)} turns, {len(calls)} calls, "
          f"{sum(len(v) for v in shots.values())} shots, across {len(RUNS)} run(s)")


STYLE = """<style>
:root{
  --ground:#f4f6f8; --panel:#ffffff; --ink:#131a22; --ink-soft:#4a5665;
  --rule:#d8dee6; --bronze:#9a6520; --bronze-soft:#f0e4d2;
  --ok:#276b5e; --ok-bg:#e4f0ec; --warn:#8d4a1e; --warn-bg:#f8e9dd;
  --bad:#8e2f2a; --bad-bg:#f8e3e1; --mono:#eef1f4;
  --shadow:0 1px 2px rgba(19,26,34,.06),0 8px 24px -16px rgba(19,26,34,.3);
}
@media (prefers-color-scheme:dark){ :root:not([data-theme="light"]){
  --ground:#0e1319; --panel:#161d26; --ink:#e7ecf2; --ink-soft:#9aa7b6;
  --rule:#27313d; --bronze:#d29a51; --bronze-soft:#2a2116;
  --ok:#7fd0bb; --ok-bg:#12271f; --warn:#e0a36a; --warn-bg:#2a1d12;
  --bad:#eb9a93; --bad-bg:#2b1514; --mono:#101720;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -16px rgba(0,0,0,.8);
}}
:root[data-theme="dark"]{
  --ground:#0e1319; --panel:#161d26; --ink:#e7ecf2; --ink-soft:#9aa7b6;
  --rule:#27313d; --bronze:#d29a51; --bronze-soft:#2a2116;
  --ok:#7fd0bb; --ok-bg:#12271f; --warn:#e0a36a; --warn-bg:#2a1d12;
  --bad:#eb9a93; --bad-bg:#2b1514; --mono:#101720;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px -16px rgba(0,0,0,.8);
}
body{background:var(--ground);color:var(--ink);
  font-family:"IBM Plex Sans",ui-sans-serif,system-ui,sans-serif;line-height:1.55;}
.wrap{max-width:52rem;margin:0 auto;padding-inline:16px;padding-block:clamp(28px,6vw,64px);}
.masthead{border-bottom:2px solid var(--ink);padding-bottom:1.6rem;margin-bottom:1.6rem;}
.eyebrow{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.7rem;
  letter-spacing:.14em;text-transform:uppercase;color:var(--bronze);margin:0 0 .7rem;}
h1{font-family:"Spectral",Georgia,serif;font-weight:600;font-size:clamp(2rem,6vw,3.1rem);
  line-height:1.04;margin:0 0 .6rem;text-wrap:balance;letter-spacing:-.015em;}
.dek{font-size:1.02rem;color:var(--ink-soft);margin:0 0 1.4rem;max-width:60ch;}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:.9rem 1.6rem;margin:0;}
.meta div{display:flex;flex-direction:column;gap:.15rem;}
.meta dt{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.66rem;
  letter-spacing:.12em;text-transform:uppercase;color:var(--ink-soft);}
.meta dd{margin:0;font-size:.9rem;}
.order-note{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.66rem;
  letter-spacing:.06em;text-transform:uppercase;color:var(--ink-soft);
  margin:0 0 .8rem;}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(105px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule);border-radius:3px;
  overflow:hidden;margin-bottom:2.4rem;}
.stat{background:var(--panel);padding:.85rem .9rem;display:flex;flex-direction:column;gap:.1rem;}
.num{font-family:"Spectral",Georgia,serif;font-size:1.7rem;font-weight:600;
  font-variant-numeric:tabular-nums;line-height:1;}
.lbl{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.62rem;
  letter-spacing:.1em;text-transform:uppercase;color:var(--ink-soft);}
.turn{margin-bottom:2.6rem;}
.turn h2{margin:0 0 1rem;font-size:.72rem;}
.tn{font-family:"IBM Plex Mono",ui-monospace,monospace;letter-spacing:.16em;
  text-transform:uppercase;color:var(--bronze);background:var(--bronze-soft);
  padding:.32rem .6rem;border-radius:2px;}
.shot{margin:0 0 1.2rem;}
.shot img{display:block;width:100%;max-width:100%;border:1px solid var(--rule);border-radius:3px;}
.shot figcaption{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.66rem;
  letter-spacing:.08em;text-transform:uppercase;color:var(--ink-soft);margin-top:.45rem;}
.reflect{background:var(--bronze-soft);border:1px solid var(--rule);border-left:3px solid var(--bronze);
  border-radius:3px;padding:.85rem 1rem;margin:0 0 .6rem;}
.rlabel{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.62rem;letter-spacing:.11em;
  text-transform:uppercase;color:var(--bronze);margin:0 0 .6rem;}
.reflect dl{margin:0;display:grid;gap:.55rem;}
.reflect dt{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.6rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-soft);margin-bottom:.1rem;}
.reflect dd{margin:0;font-family:"Spectral",Georgia,serif;font-size:.97rem;line-height:1.5;}
.think{margin:0 0 .9rem;padding:.2rem 0 .2rem 1rem;border-left:2px solid var(--bronze);
  font-family:"Spectral",Georgia,serif;font-size:1.02rem;color:var(--ink);}
.step{background:var(--panel);border:1px solid var(--rule);border-radius:3px;
  padding:.7rem .85rem;margin-bottom:.55rem;box-shadow:var(--shadow);
  display:grid;grid-template-columns:1fr auto;gap:.45rem .8rem;align-items:start;}
.step.engine_refused{border-left:3px solid var(--warn);}
.step.transport_failure,.step.mcp_error{border-left:3px solid var(--bad);}
.step.applied{border-left:3px solid var(--ok);}
.cmd{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.8rem;
  word-break:break-word;min-width:0;}
.args{color:var(--ink-soft);}
.chip{justify-self:end;display:flex;flex-direction:column;align-items:flex-end;gap:.1rem;
  font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.6rem;letter-spacing:.08em;
  text-transform:uppercase;white-space:nowrap;}
.applied .chip{color:var(--ok);} .engine_refused .chip{color:var(--warn);}
.transport_failure .chip,.mcp_error .chip{color:var(--bad);}
.dur{color:var(--ink-soft);font-variant-numeric:tabular-nums;}
.out{grid-column:1/-1;margin:.15rem 0 0;background:var(--mono);border-radius:2px;
  padding:.55rem .65rem;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.72rem;
  line-height:1.45;color:var(--ink-soft);white-space:pre-wrap;overflow-x:auto;max-height:15rem;}
footer{border-top:1px solid var(--rule);padding-top:1.1rem;margin-top:2rem;
  font-size:.8rem;color:var(--ink-soft);}
footer code{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:.75rem;}
@media (max-width:520px){ .step{grid-template-columns:1fr;} .chip{justify-self:start;align-items:flex-start;} }
</style>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Spectral:wght@500;600&display=swap">
"""

if __name__ == "__main__":
    main()
