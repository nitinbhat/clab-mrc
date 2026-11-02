#!/usr/bin/env python3
"""Build animated SVGs (SMIL, no dependencies) from the stored evidence.

Each docs/img/<fault>.svg replays a real run: top the baseline fabric, below the MRC
fabric, with a goodput chart underneath. Open the file in a browser. Everything drawn
comes from docs/evidence/<profile>/{baseline,mrc}/<fault>/sender.json and summary.json.
"""
import json
import sys
from pathlib import Path

import mrc_paths

PROJECT = Path(__file__).resolve().parent
PROFILE = "multi-plane-multi-link"
EVIDENCE = PROJECT / "docs" / "evidence" / PROFILE
OUT = PROJECT / "docs" / "img"
STEP = 0.05
W, PANEL_H = 960, 250
X = {"h1": 60, "A": 230, "S": 480, "B": 730, "h2": 900}
SPINE_Y = {(1, 1): 40, (1, 2): 95, (2, 1): 155, (2, 2): 210}
LEAF_Y = {1: 67, 2: 182}
HOST_Y = 125
COLORS = {"baseline": "#e07b00", "mrc": "#1a9850"}
TITLES = {"baseline": "BGP + ECMP fabric - one single-path flow (RoCE-style)",
          "mrc": "MRC fabric - static uSID, no BGP - one flow sprayed over 8 EVs"}
# What is broken: ("link", from, to) or ("node", name)
FAULT_MARK = {"link-down": ("link", "a1", "s11"), "silent-link": ("link", "a1", "s11"),
              "partial-loss": ("link", "a1", "s11"), "gray-switch": ("node", "s11"),
              "nic-plane": ("link", "b1", "h2")}


def node_xy(name):
    if name == "h1":
        return X["h1"], HOST_Y
    if name == "h2":
        return X["h2"], HOST_Y
    plane = int(name[1])
    if name[0] == "a":
        return X["A"], LEAF_Y[plane]
    if name[0] == "b":
        return X["B"], LEAF_Y[plane]
    return X["S"], SPINE_Y[(plane, int(name[2]))]


def path_points(plane, spine):
    return [node_xy(n) for n in ("h1", f"a{plane}", f"s{plane}{spine}", f"b{plane}", "h2")]


def load(fabric, name):
    folder = EVIDENCE / fabric / name
    if not (folder / "sender.json").exists():
        return None
    return json.loads((folder / "sender.json").read_text()), \
        json.loads((folder / "summary.json").read_text())


def bins(sender, summary, lo, hi, key):
    """Per path (plane, spine): delivered packets per second in STEP buckets."""
    n = int(round((hi - lo) / STEP))
    series = {p: [0.0] * n for p in SPINE_Y}
    for row in sender["timeline"]:
        offset = sender["t0"] + row["t"] - summary["fault_at"]
        index = int((offset - lo) / STEP)
        if not 0 <= index < n:
            continue
        for ev, count in row.get(key, {}).items():
            if sender["algo"] == "mrc":
                plane, _, spine = mrc_paths.ev_fields(int(ev, 16))
                series[(plane, spine)][index] += count / STEP
            else:
                series[None] = series.get(None, [0.0] * n)
                series[None][index] += count / STEP
    return series


def baseline_paths(series, lo, fault, repair):
    """The single flow is pinned to a1-s11; BGP moves it to s12 while that link is bad."""
    total = series.pop(None, [0.0] * len(series[(1, 1)]))
    for i, value in enumerate(total):
        t = lo + i * STEP
        path = (1, 2) if fault <= t < repair else (1, 1)
        series[path][i] = value
    return series


def anim(attr, values, n, duration, extra=""):
    keys = ";".join(f"{i / n:.4f}" for i in range(n))
    return (f'<animate attributeName="{attr}" values="{";".join(map(str, values))}" '
            f'keyTimes="{keys}" calcMode="discrete" dur="{duration}s" '
            f'repeatCount="indefinite" {extra}/>')


def panel(fabric, y0, sender, summary, series, lo, hi, name, duration):
    n = int(round((hi - lo) / STEP))
    fault, repair = 0.0, summary["repair_at"] - summary["fault_at"]
    out = [f'<g transform="translate(0,{y0})">',
           f'<text x="20" y="18" font-size="15" font-weight="bold" fill="{COLORS[fabric]}">'
           f'{TITLES[fabric]}</text>']
    for plane, spine in SPINE_Y:
        pts = " ".join(f"{x},{y}" for x, y in path_points(plane, spine))
        out.append(f'<polyline points="{pts}" fill="none" stroke="#ccc" stroke-width="2"/>')
    peak = max(max(v) for v in series.values()) or 1
    for (plane, spine), values in series.items():
        pts = " ".join(f"{x},{y}" for x, y in path_points(plane, spine))
        widths = [round(1 + 7 * v / peak, 1) if v > 0 else 0 for v in values]
        out.append(
            f'<polyline points="{pts}" fill="none" stroke="{COLORS[fabric]}" stroke-width="0" '
            f'stroke-dasharray="7 9" stroke-linecap="round">'
            f'{anim("stroke-width", widths, n, duration)}'
            '<animate attributeName="stroke-dashoffset" from="32" to="0" dur="0.5s" '
            'repeatCount="indefinite"/></polyline>')
    for name_ in ("h1", "h2", "a1", "a2", "b1", "b2", "s11", "s12", "s21", "s22"):
        x, yy = node_xy(name_)
        host = name_.startswith("h")
        out.append(f'<rect x="{x - 22}" y="{yy - 13}" width="44" height="26" rx="5" '
                   f'fill="{"#dbe9f6" if host else "#fff"}" stroke="#555"/>'
                   f'<text x="{x}" y="{yy + 5}" font-size="13" text-anchor="middle">{name_}</text>')
    mark = FAULT_MARK.get(name)
    if mark:
        if mark[0] == "link":
            (x1, y1), (x2, y2) = node_xy(mark[1]), node_xy(mark[2])
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        else:
            mx, my = node_xy(mark[1])
            mx += 28
        on = ["1" if fault <= lo + i * STEP < repair else "0" for i in range(n)]
        out.append(f'<text x="{mx}" y="{my + 8}" font-size="26" font-weight="bold" fill="#d73027" '
                   f'text-anchor="middle" opacity="0">X{anim("opacity", on, n, duration)}</text>')
    for text, start, end in captions(fabric, sender, summary, series, lo, repair):
        flag = ["1" if start <= lo + i * STEP < end else "0" for i in range(n)]
        out.append(f'<text x="20" y="{PANEL_H - 10}" font-size="14" fill="#333" opacity="0">'
                   f'{text}{anim("opacity", flag, n, duration)}</text>')
    out.append("</g>")
    return out


def captions(fabric, sender, summary, series, lo, repair):
    n = len(next(iter(series.values())))
    delivered = [sum(v[i] for v in series.values()) for i in range(n)]
    caps = [("Healthy: traffic flows", lo, 0.0), ("FAULT injected", 0.0, 0.05)]
    resume = next((lo + i * STEP for i in range(n)
                   if lo + i * STEP > 0.05 and delivered[i] > 0.5 * max(delivered[:5])), None)
    gap = summary["longest_zero_delivery_ms"]
    if fabric == "mrc":
        retired = summary.get("first_ev_retired_ms")
        caps[1] = ("FAULT injected - endpoint still sending on all paths", 0.0,
                   (retired or 0) / 1000 or 0.05)
        if retired is not None:
            caps.append((f"Sender retires the bad EVs after {retired} ms; traffic stays on good paths",
                         (retired / 1000), repair))
    elif gap > 200 and resume is not None and resume >= repair - 0.1:
        caps[1] = ("FAULT injected - BGP never notices, nothing is delivered until the repair",
                   0.0, repair)
    elif gap > 200 and resume is not None:
        caps[1] = ("FAULT injected - BGP still believes the path is fine: nothing is delivered",
                   0.0, resume)
        caps.append((f"BGP hold timer expires after {gap} ms; flow rerouted via s12", resume,
                     repair))
    else:
        caps.append(("Link down detected quickly: ECMP drops the path", 0.05, repair))
    restored = summary.get("first_ev_restored_ms")
    caps.append(("REPAIRED" + (f" - probes succeed, EVs back after {restored} ms"
                              if fabric == "mrc" and restored is not None else ""),
                 repair, repair + 2))
    return caps


def chart(y0, series_by_fabric, lo, hi, duration, repair):
    n = int(round((hi - lo) / STEP))
    left, width, height = 70, W - 110, 110
    top = y0 + 25
    peak = max(max(max(s) for s in series_by_fabric.values()), 1)
    out = [f'<text x="20" y="{y0 + 12}" font-size="15" font-weight="bold">'
           'Delivered packets per second (goodput)</text>',
           f'<rect x="{left}" y="{top}" width="{width}" height="{height}" fill="#fafafa" '
           'stroke="#999"/>']
    for fabric, values in series_by_fabric.items():
        pts = " ".join(f"{left + width * i / n:.1f},{top + height - height * v / peak:.1f}"
                       for i, v in enumerate(values))
        out.append(f'<polyline points="{pts}" fill="none" stroke="{COLORS[fabric]}" '
                   'stroke-width="2.5"/>')
    for label, t in (("fault", 0.0), ("repair", repair)):
        x = left + width * (t - lo) / (hi - lo)
        out.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + height}" '
                   f'stroke="#d73027" stroke-dasharray="4 3"/><text x="{x + 3:.1f}" y="{top + 12}" '
                   f'font-size="11" fill="#d73027">{label}</text>')
    seconds = int(lo), int(hi) + 1
    for s in range(seconds[0], seconds[1]):
        if lo <= s <= hi:
            x = left + width * (s - lo) / (hi - lo)
            out.append(f'<text x="{x:.1f}" y="{top + height + 14}" font-size="11" '
                       f'text-anchor="middle">{s:+d} s</text>')
    out.append(f'<text x="{left - 6}" y="{top + 10}" font-size="11" text-anchor="end">{int(peak)}</text>'
               f'<text x="{left - 6}" y="{top + height}" font-size="11" text-anchor="end">0</text>')
    out.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + height}" stroke="#333" '
               'stroke-width="2"><animate attributeName="x1" '
               f'values="{left};{left + width}" dur="{duration}s" repeatCount="indefinite"/>'
               f'<animate attributeName="x2" values="{left};{left + width}" dur="{duration}s" '
               'repeatCount="indefinite"/></line>')
    out.append(f'<text x="{left + 10}" y="{top + height + 30}" font-size="12" fill="{COLORS["baseline"]}">'
               f'orange = BGP + ECMP</text><text x="{left + 160}" y="{top + height + 30}" '
               f'font-size="12" fill="{COLORS["mrc"]}">green = MRC</text>')
    return out


def build(name):
    data = {f: load(f, name) for f in ("baseline", "mrc")}
    if not all(data.values()):
        return False
    base_sender, base_summary = data["baseline"]
    repair = base_summary["repair_at"] - base_summary["fault_at"]
    lo = -1.0
    hi = round(min(base_sender["duration"] - 1.0, repair + 1.0), 2)
    duration = round(hi - lo, 2)
    n = int(round((hi - lo) / STEP))
    height = 2 * PANEL_H + 230
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" '
           f'font-family="Helvetica,Arial,sans-serif" width="{W}">',
           '<rect width="100%" height="100%" fill="white"/>',
           f'<text x="20" y="26" font-size="19" font-weight="bold">{name}: '
           f'{data["mrc"][1]["description"]}</text>',
           '<text x="20" y="46" font-size="12" fill="#555">Replay of a real lab run at real time '
           '(line thickness = packets delivered on that path; dashes move when traffic flows). '
           'Planes: a1/b1 + s11/s12 = plane 1, a2/b2 + s21/s22 = plane 2.</text>']
    charts = {}
    for k, fabric in enumerate(("baseline", "mrc")):
        sender, summary = data[fabric]
        series = bins(sender, summary, lo, hi, "delivered_by_ev")
        if fabric == "baseline":
            series = baseline_paths(series, lo, 0.0, repair)
        charts[fabric] = [round(sum(v[i] for v in series.values())) for i in range(n)]
        out += panel(fabric, 60 + k * PANEL_H, sender, summary, series, lo, hi, name, duration)
    out += chart(60 + 2 * PANEL_H, charts, lo, hi, duration, repair)
    out.append("</svg>")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.svg").write_text("\n".join(out) + "\n")
    return True


def collision():
    """Static bar chart: packets carried by each of the four a-leaf uplinks (trial seed 2)."""
    data = {}
    for fabric in ("baseline", "mrc"):
        path = EVIDENCE / fabric / "ecmp-collision" / "summary.json"
        if not path.exists():
            return False
        data[fabric] = json.loads(path.read_text())
    seed = 2
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} 330" '
           'font-family="Helvetica,Arial,sans-serif" width="960">',
           '<rect width="100%" height="100%" fill="white"/>',
           '<text x="20" y="26" font-size="19" font-weight="bold">ECMP collisions: '
           '4 flows, 4 uplinks (same offered load)</text>']
    peak = 0
    for fabric in data:
        trial = next(t for t in data[fabric]["trials"] if t["seed"] == seed)
        peak = max(peak, max(trial["link_tx_packets"][k] for k in
                             ("a1:eth3", "a1:eth4", "a2:eth3", "a2:eth4")))
    for k, fabric in enumerate(("baseline", "mrc")):
        trial = next(t for t in data[fabric]["trials"] if t["seed"] == seed)
        x0 = 40 + k * 460
        label = "BGP + ECMP (random 5-tuple hash)" if fabric == "baseline" else "MRC (sprayed)"
        out.append(f'<text x="{x0}" y="62" font-size="15" font-weight="bold" '
                   f'fill="{COLORS[fabric]}">{label}</text>')
        for i, link in enumerate(("a1:eth3", "a1:eth4", "a2:eth3", "a2:eth4")):
            value = trial["link_tx_packets"][link]
            height = 170 * value / peak
            x = x0 + i * 100
            out.append(f'<rect x="{x}" y="{250 - height:.0f}" width="70" height="{height:.0f}" '
                       f'fill="{COLORS[fabric]}"/><text x="{x + 35}" y="{244 - height:.0f}" '
                       f'font-size="12" text-anchor="middle">{value}</text>'
                       f'<text x="{x + 35}" y="270" font-size="12" text-anchor="middle">{link.replace(":", " ")}</text>')
        out.append(f'<text x="{x0}" y="300" font-size="13">goodput {trial["aggregate_goodput_pps"]} of '
                   f'{data[fabric]["offered_aggregate_pps"]} pkt/s; worst flow '
                   f'{trial["worst_flow_goodput_pps"]} pkt/s</text>')
    out.append("</svg>")
    (OUT / "ecmp-collision.svg").write_text("\n".join(out) + "\n")
    return True


def main():
    names = sys.argv[1:] or list(FAULT_MARK)
    for name in names:
        print(f"{'wrote' if build(name) else 'skipped'} docs/img/{name}.svg")
    if not sys.argv[1:]:
        print(f"{'wrote' if collision() else 'skipped'} docs/img/ecmp-collision.svg")


if __name__ == "__main__":
    main()
