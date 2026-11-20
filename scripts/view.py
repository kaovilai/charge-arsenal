#!/usr/bin/env python3
"""Generate the static GitHub Pages viewer (docs/index.html).

Reads data/*.json (and reuses the match/strategy negotiation logic) and emits
a single self-contained HTML page: arsenal table, 30-minute gain chart,
best routes, multi-port allocation, measured observations, and charge curves
(rendered only when charge_curve data exists). No third-party dependencies.

Dataviz method: ranked horizontal bars in one hue for magnitude comparison;
categorical slots in fixed order for the multi-series curve chart; per-mark
hover tooltips; light and dark modes both selected from the validated
reference palette; every charted value also present in a table view.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_FILE = ROOT / "docs" / "index.html"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from match import (  # noqa: E402
    build_multiport_rows,
    charge_estimate,
    charge_targets,
    fmt_num,
    fmt_watts,
    load_measurements,
    negotiate,
)
from strategy import best_wall_watts  # noqa: E402

SHORT_LABELS = {
    "raydyn-y270": "Raydyn Y270",
    "cuktech-10": "CUKTECH 10",
    "anker-747-a2340": "Anker 747",
    "cuktech-ac65b2-65w": "CUKTECH AC65B2",
    "galaxy-z-fold7": "Galaxy Z Fold7",
    "ipad-10th-gen": "iPad 10th gen",
    "macbook-pro-14-m4-pro": "MacBook Pro 14",
    "galaxy-watch8": "Galaxy Watch8",
}

# Reference palette (validated) - light / dark pairs.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500"]


def load_json(name: str) -> dict:
    with (DATA_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def bar_path(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Horizontal bar: square at the baseline (left), 4px rounded data end."""
    if w <= r:
        return f"M{x:.1f},{y:.1f} h{w:.1f} v{h:.1f} h-{w:.1f} Z"
    return (
        f"M{x:.1f},{y:.1f} L{x + w - r:.1f},{y:.1f} "
        f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
        f"L{x + w:.1f},{y + h - r:.1f} "
        f"Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} "
        f"L{x:.1f},{y + h:.1f} Z"
    )


def gain_chart_svg(rows: list) -> str:
    """Ranked horizontal bars, one hue: watt-hours recovered in 30 min from 0%."""
    rows = sorted(rows, key=lambda r: r["wh"])
    label_w, chart_w, bar_h, row_h = 150, 470, 20, 34
    margin_t, margin_b = 8, 22
    width = label_w + chart_w + 70
    height = margin_t + row_h * len(rows) + margin_b
    max_wh = max(r["wh"] for r in rows)
    x_max = max(10.0, (int(max_wh / 10) + 1) * 10)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Watt-hours recovered in 30 minutes from 0 percent, best single-port route per device" '
        f'class="chart" xmlns="http://www.w3.org/2000/svg">'
    ]
    # gridlines + x ticks (hairline, recessive)
    tick = 0
    while tick <= x_max:
        gx = label_w + chart_w * tick / x_max
        parts.append(
            f'<line class="grid" x1="{gx:.1f}" y1="{margin_t}" x2="{gx:.1f}" '
            f'y2="{height - margin_b}"/>'
        )
        parts.append(
            f'<text class="tick" x="{gx:.1f}" y="{height - 6}" text-anchor="middle">{tick}</text>'
        )
        tick += 10
    # baseline
    parts.append(
        f'<line class="axis" x1="{label_w}" y1="{margin_t}" x2="{label_w}" '
        f'y2="{height - margin_b}"/>'
    )
    for i, row in enumerate(rows):
        y = margin_t + i * row_h + (row_h - bar_h) / 2
        w = chart_w * row["wh"] / x_max
        parts.append("<g class='row'>")
        parts.append(
            f'<text class="cat" x="{label_w - 10}" y="{y + bar_h / 2 + 4}" '
            f'text-anchor="end">{esc(row["label"])}</text>'
        )
        parts.append(
            f'<path class="bar" d="{bar_path(label_w, y, w, bar_h)}"/>'
        )
        parts.append(
            f'<text class="val" x="{label_w + w + 8:.1f}" y="{y + bar_h / 2 + 4}">'
            f"{row['wh']:.1f} Wh</text>"
        )
        parts.append(
            f'<rect class="hit" x="{label_w}" y="{margin_t + i * row_h}" '
            f'width="{chart_w}" height="{row_h}" fill="transparent" tabindex="0" '
            f'data-label="{esc(row["label"])}" '
            f'data-value="{row["wh"]:.1f} Wh in 30 min" '
            f'data-note="{esc(row["note"])}"/>'
        )
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


def curves_section(targets: list) -> str:
    """Multi-line input-watts-over-time chart; rendered only when curve data exists."""
    series = []
    for t in targets:
        curves = t.get("charge_behavior", {}).get("charge_curves")
        if not curves:
            continue
        # viewer shows the highest-peak measured curve per device
        curve = max(curves, key=lambda c: c["peak_w"])
        if curve.get("points"):
            fallback = f"{t.get('brand', '')} {t.get('model', '')}".strip()
            series.append({
                "id": t["id"],
                "label": SHORT_LABELS.get(t["id"], fallback),
                "points": curve["points"],
            })
    if not series:
        return ""
    series.sort(key=lambda s: -max(w for _, w in s["points"]))
    omitted = [s["label"] for s in series[4:]]
    series = series[:4]  # categorical adjacent-forms cap
    omitted_note = (
        f' <span class="sub">Showing the four highest-power devices; also measured: '
        f'{esc(", ".join(omitted))}.</span>'
        if omitted
        else ""
    )

    max_minutes = max(s["points"][-1][0] for s in series)
    max_watts = max(max(w for _, w in s["points"]) for s in series)
    x_max = max(30, int((max_minutes / 30) + 1) * 30)
    y_step = 25 if max_watts <= 100 else 50
    y_max = max(y_step, int((max_watts / y_step) + 1) * y_step)

    label_w, chart_w, margin_t, margin_b = 46, 560, 12, 30
    height, plot_h = 300, 240
    width = label_w + chart_w + 90

    parts = [
        '<section id="curves"><h2>Input watts over time (measured taper)</h2>',
        '<p class="sub">Watts drawn from the source through a charge, from tester '
        f"metering. Curves end at the measured full-charge time. Hover for values.{omitted_note}</p>",
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Input watts versus '
        f'minutes into a charge, per device" class="chart" id="curve-chart" '
        f'xmlns="http://www.w3.org/2000/svg">',
    ]
    # horizontal grid + watt ticks
    tick_w = 0
    while tick_w <= y_max:
        gy = margin_t + plot_h * (1 - tick_w / y_max)
        parts.append(f'<line class="grid" x1="{label_w}" y1="{gy:.1f}" x2="{label_w + chart_w}" y2="{gy:.1f}"/>')
        parts.append(f'<text class="tick" x="{label_w - 8}" y="{gy + 4:.1f}" text-anchor="end">{tick_w}W</text>')
        tick_w += y_step
    # vertical grid + minute ticks
    tick = 0
    step = 15 if x_max <= 90 else 30
    while tick <= x_max:
        gx = label_w + chart_w * tick / x_max
        parts.append(f'<line class="grid" x1="{gx:.1f}" y1="{margin_t}" x2="{gx:.1f}" y2="{margin_t + plot_h}"/>')
        parts.append(f'<text class="tick" x="{gx:.1f}" y="{margin_t + plot_h + 18}" text-anchor="middle">{tick}m</text>')
        tick += step
    parts.append(f'<line class="axis" x1="{label_w}" y1="{margin_t}" x2="{label_w}" y2="{margin_t + plot_h}"/>')
    parts.append(f'<line class="axis" x1="{label_w}" y1="{margin_t + plot_h}" x2="{label_w + chart_w}" y2="{margin_t + plot_h}"/>')
    parts.append(f'<line id="crosshair" class="grid" x1="0" y1="{margin_t}" x2="0" y2="{margin_t + plot_h}" visibility="hidden"/>')

    for i, s in enumerate(series):
        pts = " ".join(
            f"{label_w + chart_w * m / x_max:.1f},{margin_t + plot_h * (1 - min(w, y_max) / y_max):.1f}"
            for m, w in s["points"]
        )
        parts.append(f'<polyline class="line s{i + 1}" fill="none" points="{pts}"/>')
        for m, w in s["points"]:
            cx = label_w + chart_w * m / x_max
            cy = margin_t + plot_h * (1 - min(w, y_max) / y_max)
            parts.append(f'<circle class="dot s{i + 1}" cx="{cx:.1f}" cy="{cy:.1f}" r="4"/>')
        # direct end label
        m_end, w_end = s["points"][-1]
        parts.append(
            f'<text class="val" x="{label_w + chart_w * m_end / x_max + 8:.1f}" '
            f'y="{margin_t + plot_h * (1 - min(w_end, y_max) / y_max) + 4:.1f}">{esc(s["label"])}</text>'
        )
    parts.append("</svg>")

    parts.append('<ul class="legend">')
    for i, s in enumerate(series):
        parts.append(
            f'<li class="s{i + 1}"><span class="key"></span>{esc(s["label"])}</li>'
        )
    parts.append("</ul>")
    parts.append(
        '<script type="application/json" id="curve-data">'
        + json.dumps({"label_w": label_w, "chart_w": chart_w, "margin_t": margin_t, "plot_h": plot_h, "x_max": x_max, "series": series})
        + "</script>"
    )
    parts.append("</section>")
    return "".join(parts)


def html_table(headers: list, rows: list, cls: str = "") -> str:
    out = [f'<table class="{cls}">', "<thead><tr>"]
    for h in headers:
        out.append(f"<th>{esc(h)}</th>")
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for cell in row:
            if isinstance(cell, tuple):  # (text, cls)
                out.append(f'<td class="{cell[1]}">{esc(cell[0])}</td>')
            else:
                out.append(f"<td>{esc(cell)}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def best_route_rows(banks, devices, bricks, cables):
    """Best single-port route per charge target (bank or device)."""
    rows = []
    for bank in banks:
        best = None
        for brick in bricks:
            for port in brick["ports"]:
                for bank_port in bank["ports"]:
                    for cable in cables:
                        deal = negotiate(port, bank_port, cable)
                        if deal and (best is None or deal["watts"] > best[0]):
                            best = (deal["watts"], f"{brick['label']} {port['label']}", deal["profile"])
        if best:
            rows.append([SHORT_LABELS.get(bank["id"], bank["model"]), best[1], fmt_watts(best[0]),
                         f"{fmt_num(best[2]['v'])}V/{fmt_num(best[2]['a'])}A"])
    for dev in devices:
        if dev.get("charge_behavior", {}).get("max_input_w", 0) <= 0:
            continue
        watts = best_wall_watts(dev, bricks)
        rows.append([SHORT_LABELS.get(dev["id"], dev["model"]), "any USB-C brick port",
                     fmt_watts(watts), "device is the ceiling"])
    return rows


def main() -> int:
    banks = load_json("powerbanks.json")["powerbanks"]
    bricks = load_json("bricks.json")["bricks"]
    cables = load_json("cables.json")["cables"]
    devices = load_json("devices.json")["devices"]
    measurements = load_measurements()
    targets = charge_targets(banks, devices)
    multiport_rows = build_multiport_rows(bricks, targets, measurements)

    # gain chart rows
    gain_rows = []
    all_targets = devices + banks
    for t in all_targets:
        if not t.get("charge_behavior", {}).get("max_input_w"):
            continue
        watts = best_wall_watts(t, bricks)
        wh = charge_estimate(t, watts)["wh_per_30"]
        gain_rows.append({
            "label": SHORT_LABELS.get(t["id"], f"{t.get('brand', '')} {t.get('model', '')}".strip()),
            "wh": wh,
            "note": f"best wall route {fmt_watts(watts)}",
        })

    arsenal_rows = []
    for item in banks + bricks + devices:
        role = "power bank" if item in banks else "wall brick" if item in bricks else "device"
        in_out = []
        if item.get("charge_behavior", {}).get("max_input_w"):
            in_out.append(f"in {fmt_num(item['charge_behavior']['max_input_w'])}W")
        if item.get("total_output_w"):
            in_out.append(f"out {fmt_num(item['total_output_w'])}W")
        elif role == "wall brick":
            port_max = max(
                (p.get("output", {}).get("max_w", 0) for p in item.get("ports", [])),
                default=0,
            )
            if port_max:
                in_out.append(f"out {fmt_num(port_max)}W max port")
        arsenal_rows.append([
            SHORT_LABELS.get(item["id"], f"{item.get('brand', '')} {item.get('model', '')}".strip()),
            role, " / ".join(in_out) or "-",
        ])

    port_labels = {}
    for brick in bricks:
        for port in brick.get("ports", []):
            port_labels[(brick["id"], port["id"])] = port["label"]
    combo_rows = []
    for brick in bricks:
        mp = brick.get("multiport_allocation") or {}
        for combo in mp.get("combos", []):
            def map_part(part):
                chunks = []
                for chunk in part.split("+"):
                    names = [
                        port_labels.get((brick["id"], t.strip()), t.strip())
                        for t in chunk.split(" or ")
                    ]
                    chunks.append(" or ".join(names))
                return " + ".join(chunks)

            ports = " + ".join(map_part(p) for p in combo["ports"])
            combo_rows.append([
                SHORT_LABELS.get(brick["id"], brick["label"]),
                ports,
                f"{fmt_num(combo.get('total_w'))}W total",
                combo.get("per_port_split") or combo.get("profile") or "dynamic",
            ])

    scenario_rows = []
    for r in multiport_rows:
        measured = f"~{r['measured_secondary_w']}W" if r["measured_secondary_w"] else "-"
        scenario_rows.append([
            r["primary"]["label"], r["secondary"]["label"],
            fmt_watts(r["primary_w"]), fmt_watts(r["secondary_w"]),
            (measured, "measured") if r["measured_secondary_w"] else measured,
        ])

    measurement_rows = []
    for m in measurements:
        reading = m.get("reading", {})
        measurement_rows.append([
            m.get("date", "-"),
            SHORT_LABELS.get(reading.get("device", ""), reading.get("device", "-")),
            SHORT_LABELS.get(m.get("setup", {}).get("charger", ""), m.get("setup", {}).get("charger", "-")),
            f"~{reading.get('watts')}W @ {reading.get('volts')}V",
            m.get("source", "-"),
        ])

    route_rows = best_route_rows(banks, devices, bricks, cables)
    curves = curves_section(banks + devices)

    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Charge Arsenal</title>
<style>
.viz-root {{
  color-scheme: light;
  --page: #f9f9f7; --surface-1: #fcfcfb;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --s1: {SERIES[0]}; --s2: {SERIES[1]}; --s3: {SERIES[2]}; --s4: {SERIES[3]};
  --bar: {SERIES[0]};
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) .viz-root {{
    color-scheme: dark;
    --page: #0d0d0d; --surface-1: #1a1a19;
    --text-primary: #f0efec; --text-secondary: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --s1: {SERIES_DARK[0]}; --s2: {SERIES_DARK[1]}; --s3: {SERIES_DARK[2]}; --s4: {SERIES_DARK[3]};
    --bar: {SERIES_DARK[0]};
  }}
}}
:root[data-theme="dark"] .viz-root {{
  color-scheme: dark;
  --page: #0d0d0d; --surface-1: #1a1a19;
  --text-primary: #f0efec; --text-secondary: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --s1: {SERIES_DARK[0]}; --s2: {SERIES_DARK[1]}; --s3: {SERIES_DARK[2]}; --s4: {SERIES_DARK[3]};
  --bar: {SERIES_DARK[0]};
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--page); }}
.viz-root {{
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  color: var(--text-primary); max-width: 860px; margin: 0 auto;
  padding: 32px 20px 64px; line-height: 1.5;
}}
h1 {{ font-size: 24px; margin: 0 0 4px; }}
h2 {{ font-size: 17px; margin: 40px 0 4px; }}
h3 {{ font-size: 14px; margin: 24px 0 4px; color: var(--text-secondary); }}
.sub {{ color: var(--text-secondary); font-size: 13px; margin: 0 0 14px; }}
header {{ display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }}
header p {{ color: var(--text-secondary); margin: 0; font-size: 14px; }}
#theme-toggle {{
  margin-left: auto; background: var(--surface-1); color: var(--text-secondary);
  border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px;
  font: inherit; font-size: 12px; cursor: pointer;
}}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; background: var(--surface-1);
  border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
th {{ text-align: left; color: var(--text-secondary); font-weight: 600; padding: 8px 10px;
  border-bottom: 1px solid var(--grid); }}
td {{ padding: 6px 10px; border-bottom: 1px solid var(--grid); color: var(--text-primary); }}
tbody tr:last-child td {{ border-bottom: none; }}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
td.measured {{ font-weight: 600; }}
.chart {{ width: 100%; height: auto; display: block; background: var(--surface-1);
  border: 1px solid var(--border); border-radius: 8px; }}
.grid {{ stroke: var(--grid); stroke-width: 1; }}
.axis {{ stroke: var(--axis); stroke-width: 1; }}
.tick {{ fill: var(--muted); font-size: 10px; font-family: inherit; }}
.cat {{ fill: var(--text-primary); font-size: 12px; font-family: inherit; }}
.val {{ fill: var(--text-secondary); font-size: 11px; font-family: inherit; }}
.bar {{ fill: var(--bar); }}
.row:hover .bar, .row:focus-within .bar {{ opacity: 0.82; }}
.hit {{ cursor: default; outline: none; }}
.line {{ stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }}
.s1 {{ stroke: var(--s1); fill: var(--s1); }} .s2 {{ stroke: var(--s2); fill: var(--s2); }}
.s3 {{ stroke: var(--s3); fill: var(--s3); }} .s4 {{ stroke: var(--s4); fill: var(--s4); }}
.dot {{ stroke: var(--surface-1); stroke-width: 2; }}
.legend {{ list-style: none; display: flex; gap: 18px; flex-wrap: wrap;
  padding: 0; margin: 10px 0 0; font-size: 12px; color: var(--text-secondary); }}
.legend .key {{ display: inline-block; width: 14px; height: 2px; border-radius: 1px;
  margin-right: 6px; vertical-align: middle; }}
.legend li.s1 .key {{ background: var(--s1); }} .legend li.s2 .key {{ background: var(--s2); }}
.legend li.s3 .key {{ background: var(--s3); }} .legend li.s4 .key {{ background: var(--s4); }}
#tooltip {{
  position: fixed; pointer-events: none; z-index: 10; display: none;
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 6px;
  padding: 6px 10px; font-size: 12px; color: var(--text-primary);
  box-shadow: 0 2px 8px rgba(0,0,0,0.12); max-width: 260px;
}}
#tooltip .t-value {{ font-weight: 600; }}
#tooltip .t-note {{ color: var(--text-secondary); }}
footer {{ margin-top: 48px; color: var(--muted); font-size: 12px; }}
a {{ color: var(--s1); }}
</style>
</head>
<body>
<div class="viz-root">
<header>
  <h1>Charge Arsenal</h1>
  <p>which port into which, to move the most watts in the least time</p>
  <button id="theme-toggle" aria-label="Toggle color theme">Toggle theme</button>
</header>

<h2>Arsenal</h2>
{html_table(["Device", "Role", "Power"], arsenal_rows)}

<h2>30 minutes at the wall, from 0%</h2>
<p class="sub">Watt-hours recovered per device on its best single-port route, conversion
losses included. Every value is also in the table below.</p>
{gain_chart_svg(gain_rows)}
{html_table(["Device", "Best route", "Negotiated", "Profile"], route_rows)}

<h2>Multi-port allocation</h2>
<p class="sub">Shared-budget bricks keep the negotiated voltage but change the granted
current. Measured values beat the model.</p>
{html_table(["Brick", "Ports in use", "Published cap", "Split"], combo_rows)}
<h3>Two-device scenarios (Anker 747)</h3>
{html_table(["Primary (first port)", "Secondary (second port)", "Model: primary", "Model: secondary", "Measured secondary"], scenario_rows)}

<h2>Measured observations</h2>
{html_table(["Date", "Device", "Charger", "Reading", "Source"], measurement_rows)}
{curves}

<footer>Generated by <code>scripts/view.py</code> from <code>data/*.json</code> — edit the
JSON, never this file. Multi-port and negotiation logic shared with
<code>scripts/match.py</code>.</footer>
</div>
<div id="tooltip" role="status"></div>
<script>
(function () {{
  var tooltip = document.getElementById('tooltip');

  function show(clientX, clientY, rows) {{
    tooltip.textContent = '';
    rows.forEach(function (r) {{
      var line = document.createElement('div');
      var value = document.createElement('span');
      value.className = 't-value';
      value.textContent = r.value;
      line.appendChild(value);
      if (r.label) {{
        var label = document.createElement('span');
        label.className = 't-note';
        label.textContent = ' ' + r.label;
        line.appendChild(label);
      }}
      tooltip.appendChild(line);
      if (r.note) {{
        var note = document.createElement('div');
        note.className = 't-note';
        note.textContent = r.note;
        tooltip.appendChild(note);
      }}
    }});
    tooltip.style.display = 'block';
    var x = clientX + 12, y = clientY + 12;
    var box = tooltip.getBoundingClientRect();
    if (x + box.width > window.innerWidth - 8) x = clientX - box.width - 12;
    if (y + box.height > window.innerHeight - 8) y = clientY - box.height - 12;
    tooltip.style.left = x + 'px';
    tooltip.style.top = y + 'px';
  }}
  function hide() {{ tooltip.style.display = 'none'; }}

  document.querySelectorAll('.hit').forEach(function (bar) {{
    function rows() {{
      return [
        {{ value: bar.dataset.value, label: bar.dataset.label }},
        {{ value: '', note: bar.dataset.note || '' }}
      ];
    }}
    bar.addEventListener('pointerenter', function (e) {{ show(e.clientX, e.clientY, rows()); }});
    bar.addEventListener('pointermove', function (e) {{ show(e.clientX, e.clientY, rows()); }});
    bar.addEventListener('pointerleave', hide);
    bar.addEventListener('focus', function () {{
      var rect = bar.getBoundingClientRect();
      show(rect.left + rect.width / 2, rect.top, rows());
    }});
    bar.addEventListener('blur', hide);
  }});

  var chart = document.getElementById('curve-chart');
  var dataEl = document.getElementById('curve-data');
  if (chart && dataEl) {{
    var data = JSON.parse(dataEl.textContent);
    var crosshair = document.getElementById('crosshair');
    function onPoint(e) {{
      var rect = chart.getBoundingClientRect();
      var scale = (rect.width / chart.viewBox.baseVal.width);
      var px = (e.clientX - rect.left) / scale;
      var minute = Math.round(Math.max(0, Math.min(data.x_max,
        (px - data.label_w) / data.chart_w * data.x_max)));
      crosshair.setAttribute('x1', data.label_w + data.chart_w * minute / data.x_max);
      crosshair.setAttribute('x2', data.label_w + data.chart_w * minute / data.x_max);
      crosshair.setAttribute('visibility', 'visible');
      var rows = data.series.map(function (s) {{
        if (minute > s.points[s.points.length - 1][0]) {{
          return {{ value: 'full', label: s.label + ' @ ' + s.points[s.points.length - 1][0] + 'm' }};
        }}
        var best = s.points[0];
        s.points.forEach(function (p) {{
          if (Math.abs(p[0] - minute) < Math.abs(best[0] - minute)) best = p;
        }});
        return {{ value: best[1] + 'W', label: s.label + ' @ ' + minute + 'm' }};
      }});
      show(e.clientX, e.clientY, rows);
    }}
    chart.addEventListener('pointermove', onPoint);
    chart.addEventListener('pointerleave', function () {{
      crosshair.setAttribute('visibility', 'hidden');
      hide();
    }});
  }}

  var toggle = document.getElementById('theme-toggle');
  toggle.addEventListener('click', function () {{
    var root = document.documentElement;
    var current = root.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    root.dataset.theme = current === 'dark' ? 'light' : 'dark';
  }});
}})();
</script>
</body>
</html>
"""
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(doc, encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE} ({len(gain_rows)} gain rows, {len(scenario_rows)} scenarios, "
          f"{len(measurement_rows)} measurements, curves {'on' if curves else 'off'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
