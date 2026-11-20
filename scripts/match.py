#!/usr/bin/env python3
"""Compute optimal charging pairings for the charge-arsenal database.

Reads data/{powerbanks,bricks,cables,devices}.json and regenerates MATCHES.md with:
  1. The best brick port -> bank port + cable combo for recharging each bank.
  2. The best brick or bank port -> device port + cable combo for charging devices.

No third-party dependencies.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_FILE = ROOT / "MATCHES.md"


def load_json(name: str) -> dict:
    with (DATA_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def profile_watts(profile: dict) -> float:
    return profile["v"] * profile["a"]


def fmt_num(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def fmt_profile(profile: dict) -> str:
    return f"{fmt_num(profile['v'])}V/{fmt_num(profile['a'])}A"


def fmt_watts(value: float) -> str:
    rounded = round(value)
    if abs(value - rounded) < 0.05:
        return f"{rounded}W"
    return f"{value:.1f}W"


def pps_deal(source_port: dict, sink_port: dict, cable: dict) -> dict | None:
    """PPS/APDO deal for sinks that need Samsung-style PPS for full speed.

    The sink declares its PPS requirement on the port (`pps` block); the source
    advertises `pps_profiles` (voltage window + amps). The operating point is
    the measured contract (e.g. Z Fold7: ~9V/2.5A), capped by the source's PPS
    current and the cable.
    """
    sink_pps = sink_port.get("pps")
    offers = source_port.get("pps_profiles") or []
    if not sink_pps or not offers:
        return None
    v_op = sink_pps.get("v_operate", 9)
    a_op = sink_pps.get("a_operate", sink_pps.get("a_needed", 0))
    a_needed = sink_pps.get("a_needed", 0)
    best = None
    for apdo in offers:
        if apdo.get("v_max", 0) < v_op or apdo.get("v_min", 0) > v_op:
            continue  # operating point outside this PPS window
        if a_needed and apdo.get("a", 0) < a_needed:
            continue  # APDO cannot deliver the current the sink requires for SFC
        if cable.get("max_v", 0) < v_op:
            continue
        current = min(a_op, apdo.get("a", 0), cable.get("max_a", float("inf")))
        if current <= 0:
            continue
        watts = min(
            v_op * current,
            sink_port.get("input", {}).get("max_w", float("inf")),
            source_port.get("output", {}).get("max_w", float("inf")),
        )
        if best is None or watts > best["watts"]:
            best = {"watts": watts, "profile": {"v": v_op, "a": current, "pps": True}}
    return best


def negotiate(source_port: dict, sink_port: dict, cable: dict) -> dict | None:
    """Best source-to-sink deal: highest shared PD profile the cable can carry.

    When the sink declares a PPS requirement (Samsung Super Fast Charging), the
    PPS deal is preferred - without PPS those phones fall back to a slower
    fixed-PD contract.
    """
    source_output = source_port.get("output")
    sink_input = sink_port.get("input")
    if not source_output or not sink_input:
        return None
    if source_port.get("type") != "usb-c" or sink_port.get("type") != "usb-c":
        return None
    if cable.get("connector") != "usb-c":
        return None

    best = None
    for wanted in sink_input.get("pd_profiles", []):
        rail_v = wanted["v"]
        for offered in source_output.get("pd_profiles", []):
            if offered["v"] != rail_v:
                continue
            if cable.get("max_v", 0) < rail_v:
                continue
            # PD sinks may draw less than their maximum at a given voltage,
            # so the current is limited by whichever end (or cable) is weakest.
            current = min(
                wanted["a"],
                offered["a"],
                cable.get("max_a", float("inf")),
            )
            if current <= 0:
                continue
            watts = min(
                rail_v * current,
                source_output.get("max_w", float("inf")),
                sink_input.get("max_w", float("inf")),
            )
            better = best is None or watts > best["watts"] or (
                watts == best["watts"] and rail_v > best["profile"]["v"]
            )
            if better:
                best = {"watts": watts, "profile": {"v": rail_v, "a": current}}
    pps = pps_deal(source_port, sink_port, cable)
    if (sink_port.get("pps") or {}).get("required"):
        if pps:
            return pps
        if best:
            # Required PPS unavailable (e.g. the Y270, whose PPS is
            # undocumented): conservative Samsung fixed-PD fallback.
            fallback_w = min(best["watts"], 15.0)
            v = best["profile"]["v"]
            return {
                "watts": fallback_w,
                "profile": {
                    "v": v,
                    "a": round(fallback_w / v, 2),
                    "fallback": "no PPS source: conservative 15W fallback",
                },
            }
        return None
    if pps and (best is None or pps["watts"] > best["watts"]):
        return pps
    return best


def curve_watts(points: list, minute: float) -> float:
    """Piecewise-linear input watts at a given minute into the charge."""
    if minute <= points[0][0]:
        return points[0][1]
    for (m0, w0), (m1, w1) in zip(points, points[1:]):
        if minute <= m1:
            span = m1 - m0
            if span <= 0:
                return w1
            return w0 + (w1 - w0) * (minute - m0) / span
    return points[-1][1]


def pick_curve(behavior: dict, available_w: float) -> dict | None:
    """Largest measured curve whose peak the source can actually feed."""
    curves = behavior.get("charge_curves") or []
    if not curves:
        return None
    fitting = [c for c in curves if c["peak_w"] <= available_w * 1.02]
    if fitting:
        return max(fitting, key=lambda c: c["peak_w"])
    return min(curves, key=lambda c: c["peak_w"])


def curve_estimate(
    behavior: dict, available_w: float, capacity_wh: float, efficiency: float
) -> dict | None:
    """Integrate a measured taper curve instead of assuming flat input watts."""
    curve = pick_curve(behavior, available_w)
    if not curve or capacity_wh <= 0:
        return None
    # original points; the source cap is applied at evaluation time so it
    # never introduces taper before the curve's own draw reaches the cap
    points = curve["points"]

    step = 0.5  # minutes
    first_30_wh = 0.0
    total_wh = 0.0
    hours = None
    t = 0.0
    last_minute = points[-1][0]
    while t < last_minute + step:
        w = min(curve_watts(points, t), available_w) * efficiency  # watts into the battery
        energy = w * step / 60.0
        if t < 30:
            first_30_wh += min(energy, max(0.0, 30 - t) * w / 60.0)
        total_wh += energy
        if hours is None and total_wh >= capacity_wh:
            hours = t / 60.0
        t += step
    avg_w = first_30_wh / 0.5 if first_30_wh else 0.0
    if hours is None:
        # curve ran out before a full charge; extrapolate at the final rate
        remaining = capacity_wh - total_wh
        tail = max(curve_watts(points, last_minute) * efficiency, 1.0)
        minutes = t + remaining / tail * 60.0
        hours = minutes / 60.0
    return {
        "effective_w": avg_w,
        "hours": hours,
        "wh_per_30": min(first_30_wh, capacity_wh),
        "pct_per_30": min(100.0, 100.0 * first_30_wh / capacity_wh),
        "curve_peak_w": curve["peak_w"],
    }


def select_soc_curve(behavior: dict, available_w: float):
    """Pick the SOC curve whose peak the source can feed; return it scaled."""
    soc_curves = behavior.get("soc_curves") or []
    if not soc_curves:
        return None
    for c in soc_curves:
        c.setdefault("peak_w", max(w for _, w in c["points"]))
    fitting = [c for c in soc_curves if c["peak_w"] <= available_w * 1.02]
    curve = (
        max(fitting, key=lambda c: c["peak_w"])
        if fitting
        else min(soc_curves, key=lambda c: c["peak_w"])
    )
    # return the original points; callers clamp at evaluation time so the
    # source cap never introduces taper before the curve's own draw reaches it
    return curve, list(curve["points"])


def soc_time_to_full(target: dict, available_w: float) -> float | None:
    """Hours to 100% from empty, integrating the SOC curve; None without data."""
    behavior = target["charge_behavior"]
    capacity_wh = target["capacity_wh"]
    efficiency = behavior.get("avg_input_efficiency", 0.85)
    selected = select_soc_curve(behavior, available_w)
    if not selected or capacity_wh <= 0:
        return None
    _, points = selected
    step = 0.25
    soc = 0.0
    t = 0.0
    while t < 600 and soc < 100:
        w = min(curve_watts(points, soc), available_w) * efficiency
        dwh = w * step / 60.0
        if dwh <= 0:
            return None
        soc += 100.0 * dwh / capacity_wh
        t += step
    return t / 60.0 if soc >= 100 else None


def soc_gain(
    target: dict,
    start_soc: float,
    available_w: float,
    minutes: float = 30.0,
) -> dict | None:
    """Energy into the battery over `minutes`, starting from `start_soc`.

    Uses the measured SOC->watts curves when present (the taper is a function
    of state of charge, so gains from a half-full battery differ from empty),
    falling back to the time-based curve model, then to flat input watts.
    Returns {wh, end_soc, effective_w} or None when no curve data exists.
    """
    behavior = target["charge_behavior"]
    capacity_wh = target["capacity_wh"]
    efficiency = behavior.get("avg_input_efficiency", 0.85)
    if capacity_wh <= 0:
        return None
    soc_curves = behavior.get("soc_curves") or []
    time_curves = behavior.get("charge_curves") or []
    if soc_curves:
        selected = select_soc_curve(behavior, available_w)
        if selected is None:
            return None
        _, points = selected
        step = 0.25  # minutes
        soc = start_soc
        gained = 0.0
        t = 0.0
        while t < minutes and soc < 100:
            dt = min(step, minutes - t)  # never integrate past the request
            w = min(curve_watts(points, soc), available_w) * efficiency
            dwh = w * dt / 60.0
            remaining = capacity_wh * (1 - soc / 100.0)
            dwh = min(dwh, max(0.0, remaining))
            soc += 100.0 * dwh / capacity_wh
            gained += dwh
            t += dt
        return {
            "wh": gained,
            "end_soc": min(100.0, soc),
            "effective_w": gained / (minutes / 60.0) if minutes else 0.0,
        }
    if time_curves:
        # time-based curve can only model a charge from empty
        if start_soc > 0:
            return None
        curve = pick_curve(behavior, available_w)
        if curve is None:
            return None
        pts = curve["points"]
        step = 0.25
        t = 0.0
        gained = 0.0
        while t < minutes:
            dt = min(step, minutes - t)
            gained += min(curve_watts(pts, t), available_w) * efficiency * dt / 60.0
            t += dt
        gained = min(gained, capacity_wh)
        return {
            "wh": gained,
            "end_soc": min(100.0, 100.0 * gained / capacity_wh),
            "effective_w": gained / (minutes / 60.0) if minutes else 0.0,
        }
    return None


def charge_estimate(target: dict, watts: float) -> dict:
    behavior = target["charge_behavior"]
    capacity_wh = target["capacity_wh"]
    efficiency = behavior.get("avg_input_efficiency", 0.85)
    gain = soc_gain(target, 0.0, watts, 30.0)
    if gain:
        if behavior.get("soc_curves"):
            hours = soc_time_to_full(target, watts)
            peak = select_soc_curve(behavior, watts)[0]["peak_w"]
        else:
            curved = curve_estimate(behavior, watts, capacity_wh, efficiency)
            hours = curved["hours"] if curved else None
            peak = curved["curve_peak_w"] if curved else None
        if hours is not None:
            return {
                "effective_w": gain["effective_w"],
                "hours": hours,
                "wh_per_30": min(gain["wh"], capacity_wh),
                "pct_per_30": min(100.0, gain["end_soc"]),
                "curve_peak_w": peak,
            }
    curved = curve_estimate(behavior, watts, capacity_wh, efficiency)
    if curved:
        return curved
    effective_w = watts * efficiency
    hours = capacity_wh / effective_w if effective_w > 0 else None
    wh_per_30 = min(effective_w * 0.5, capacity_wh)
    pct_per_30 = min(100.0, 100.0 * wh_per_30 / capacity_wh)
    return {
        "effective_w": effective_w,
        "hours": hours,
        "wh_per_30": wh_per_30,
        "pct_per_30": pct_per_30,
        "curve_peak_w": None,
    }


def load_measurements() -> list:
    path = DATA_DIR / "measurements.json"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return json.load(fh).get("measurements", [])


def pair_allocation(brick: dict, want_primary: float, want_secondary: float) -> tuple:
    """First-order two-USB-C-port allocation for a shared-budget brick.

    Verified rules for the Anker 747 (A2340):
    - any single USB-C port can grant up to its per-port cap (100W)
    - two USB-C ports share the total budget (150W)
    - when both sinks want at least the balanced-split point (75W), each is
      capped there (chongdiantou measured 71.6W + 67.6W with two MacBooks)
    - otherwise the primary keeps its draw and the secondary gets the remainder

    Dynamic allocation can do worse than this estimate (user-measured 25W to
    the Y270 behind a charging MacBook, where the model says 54W); measured
    values in data/measurements.json always win over the model.
    """
    mp = brick.get("multiport_allocation") or {}
    budget = mp.get("total_budget_w", 0)
    port_cap = mp.get("per_port_cap_w", 0)
    balance = mp.get("balanced_split_w", budget / 2 if budget else 0)
    if not budget or not port_cap:
        return want_primary, want_secondary
    if want_primary >= balance and want_secondary >= balance:
        return min(want_primary, balance), min(want_secondary, balance)
    primary = min(want_primary, port_cap)
    secondary = min(want_secondary, port_cap, budget - primary)
    return primary, secondary


def charge_targets(banks: list, devices: list) -> list:
    """Everything in the arsenal that can sit on a brick port as a sink."""
    targets = []
    for bank in banks:
        targets.append(
            {
                "id": bank["id"],
                "label": f"{bank['brand']} {bank['model']}",
                "max_w": bank.get("charge_behavior", {}).get("max_input_w", 0),
            }
        )
    for dev in devices:
        targets.append(
            {
                "id": dev["id"],
                "label": f"{dev['brand']} {dev['model']}",
                "max_w": dev.get("charge_behavior", {}).get("max_input_w", 0),
            }
        )
    return targets


def build_multiport_rows(bricks: list, targets: list, measurements: list) -> list:
    """Two-device scenarios for every shared-budget brick in the arsenal."""
    rows = []
    for brick in bricks:
        mp = brick.get("multiport_allocation") or {}
        if not mp.get("total_budget_w"):
            continue  # combos documented but fixed per-revision: no modeled scenarios
        for primary in targets:
            for secondary in targets:
                if primary["id"] == secondary["id"]:
                    continue
                p_w, s_w = pair_allocation(brick, primary["max_w"], secondary["max_w"])
                measured = None
                for m in measurements:
                    setup = m.get("setup", {})
                    if (
                        setup.get("charger") == brick["id"]
                        and m.get("reading", {}).get("device") == secondary["id"]
                        and setup.get("port_1_device") == primary["id"]
                    ):
                        measured = m["reading"]["watts"]
                rows.append(
                    {
                        "brick": brick,
                        "primary": primary,
                        "secondary": secondary,
                        "primary_w": p_w,
                        "secondary_w": s_w,
                        "measured_secondary_w": measured,
                    }
                )
    rows.sort(key=lambda r: (-r["secondary_w"], -r["primary_w"]))
    return rows


def render_allocation_combos(bricks: list) -> str:
    lines = [
        "| Brick | Ports in use | Published cap | Split |",
        "| --- | --- | --- | --- |",
    ]
    for brick in bricks:
        mp = brick.get("multiport_allocation") or {}
        for combo in mp.get("combos", []):
            ports = " + ".join(combo["ports"])
            total = combo.get("total_w")
            split = combo.get("per_port_split") or combo.get("profile") or "dynamic"
            cap = f"{fmt_num(total)}W total" if total else ""
            lines.append(f"| {brick['label']} | {ports} | {cap} | {split} |")
    return "\n".join(lines)


def render_multiport_table(rows: list) -> str:
    lines = [
        "| Brick | Primary (first port) | Secondary (second port) | Model: primary | Model: secondary | Measured secondary |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        measured = (
            f"~{row['measured_secondary_w']}W" if row["measured_secondary_w"] else "-"
        )
        lines.append(
            "| {brick} | {primary} | {secondary} | {pw} | {sw} | {measured} |".format(
                brick=row["brick"]["label"],
                primary=row["primary"]["label"],
                secondary=row["secondary"]["label"],
                pw=fmt_watts(row["primary_w"]),
                sw=fmt_watts(row["secondary_w"]),
                measured=measured,
            )
        )
    return "\n".join(lines)


def build_input_rows(banks: list, bricks: list, cables: list) -> list:
    rows = []
    for bank in banks:
        for brick in bricks:
            for brick_port in brick["ports"]:
                for bank_port in bank["ports"]:
                    for cable in cables:
                        deal = negotiate(brick_port, bank_port, cable)
                        if not deal:
                            continue
                        est = charge_estimate(bank, deal["watts"])
                        rows.append(
                            {
                                "bank": bank,
                                "brick": brick,
                                "brick_port": brick_port,
                                "bank_port": bank_port,
                                "cable": cable,
                                "watts": deal["watts"],
                                "profile": deal["profile"],
                                **est,
                            }
                        )
    rows.sort(key=lambda r: (-r["watts"], r["hours"]))
    return rows


def build_device_rows(devices: list, bricks: list, banks: list, cables: list) -> list:
    rows = []
    sources = []
    for brick in bricks:
        for port in brick["ports"]:
            sources.append({"kind": "brick", "device": brick, "port": port})
    for bank in banks:
        for port in bank["ports"]:
            sources.append({"kind": "bank", "device": bank, "port": port})

    for device in devices:
        for device_port in device["ports"]:
            if device_port.get("type") != "usb-c":
                continue
            for source in sources:
                source_port = source["port"]
                if source_port.get("type") != "usb-c":
                    continue
                for cable in cables:
                    deal = negotiate(source_port, device_port, cable)
                    if not deal:
                        continue
                    est = charge_estimate(device, deal["watts"])
                    rows.append(
                        {
                            "device": device,
                            "device_port": device_port,
                            "source_kind": source["kind"],
                            "source": source["device"],
                            "source_port": source_port,
                            "cable": cable,
                            "watts": deal["watts"],
                            "profile": deal["profile"],
                            **est,
                        }
                    )
    rows.sort(key=lambda r: -r["watts"])
    return rows


def render_input_table(rows: list) -> str:
    lines = [
        "| Brick | Brick port | Bank port | Cable | Negotiated | Profile | Est. full | Wh / 30 min | % / 30 min |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        brick_tag = " *(example)*" if row["brick"].get("data_confidence") == "archetype" else ""
        lines.append(
            "| {brick}{tag} | {bport} | {port} | {cable} | {watts} | {profile} | {hours} | {wh} | {pct} |".format(
                brick=row["brick"]["label"],
                tag=brick_tag,
                bport=row["brick_port"]["label"],
                port=row["bank_port"]["label"],
                cable=row["cable"]["label"],
                watts=fmt_watts(row["watts"]),
                profile=fmt_profile(row["profile"]),
                hours=f"{row['hours']:.1f} h",
                wh=fmt_num(round(row["wh_per_30"], 1)),
                pct=f"{row['pct_per_30']:.0f}%",
            )
        )
    return "\n".join(lines)


def render_device_table(rows: list) -> str:
    lines = [
        "| Device | Source | Source port | Device port | Cable | Negotiated | Profile | Est. full | Wh / 30 min |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {device} | {source} | {source_port} | {device_port} | {cable} | {watts} | {profile} | {hours} | {wh} |".format(
                device=row["device"]["model"],
                source=row["source"]["label"] if row["source_kind"] == "brick" else f"{row['source']['brand']} {row['source']['model']}",
                source_port=row["source_port"]["label"],
                device_port=row["device_port"]["label"],
                cable=row["cable"]["label"],
                watts=fmt_watts(row["watts"]),
                profile=fmt_profile(row["profile"]),
                hours=f"{row['hours']:.1f} h",
                wh=fmt_num(round(row["wh_per_30"], 1)),
            )
        )
    return "\n".join(lines)


def render_device_bottom_line(devices: list, device_rows: list) -> list:
    sections = []
    for device in devices:
        rows = [r for r in device_rows if r["device"]["id"] == device["id"]]
        if not rows:
            notes = device.get("charge_behavior", {}).get("notes", "")
            sections.append(
                [
                    f"### {device['brand']} {device['model']}",
                    "",
                    f"- **No USB-C route in this arsenal:** {notes}",
                    "",
                ]
            )
            continue

        best_wall = next((r for r in rows if r["source_kind"] == "brick"), None)
        best_bank = next((r for r in rows if r["source_kind"] == "bank"), None)
        section = [
            f"### {device['brand']} {device['model']}",
            "",
        ]
        if best_wall:
            section.extend(
                [
                    f"- **Best wall route:** {best_wall['source']['label']} {best_wall['source_port']['label']}"
                    f" -> {best_wall['device_port']['label']} with the {best_wall['cable']['label']}:"
                    f" **{fmt_watts(best_wall['watts'])}** ({fmt_profile(best_wall['profile'])}).",
                    f"- **Estimated time to full:** ~{best_wall['hours']:.1f} h"
                    f" ({fmt_watts(best_wall['effective_w'])} first-30-min average input).",
                    f"- **30 minutes at the wall buys:** ~{fmt_num(round(best_wall['wh_per_30'], 1))} Wh"
                    f" (~{best_wall['pct_per_30']:.0f}% of the battery). Early charging is faster than this average.",
                ]
            )
        if best_bank:
            section.extend(
                [
                    f"- **Best bank route:** {best_bank['source']['brand']} {best_bank['source']['model']}"
                    f" {best_bank['source_port']['label']} -> {best_bank['device_port']['label']}"
                    f" with the {best_bank['cable']['label']}: **{fmt_watts(best_bank['watts'])}**"
                    f" ({fmt_profile(best_bank['profile'])}).",
                ]
            )
        section.extend(["", device.get("charge_behavior", {}).get("notes", ""), ""])
        sections.append(section)
    return sections


def render_bottom_line(banks: list, input_rows: list) -> list:
    sections = []
    for bank in banks:
        bank_rows = [r for r in input_rows if r["bank"]["id"] == bank["id"]]
        if not bank_rows:
            continue
        best = bank_rows[0]
        sections.append(
            [
                f"### {bank['brand']} {bank['model']}",
                "",
                f"- **Best recharge combo:** {best['brick']['label']} {best['brick_port']['label']}"
                f" -> {best['bank_port']['label']} with the {best['cable']['label']}:"
                f" **{fmt_watts(best['watts'])}** ({fmt_profile(best['profile'])}).",
                f"- **Estimated time to full:** ~{best['hours']:.1f} h"
                f" ({fmt_watts(best['effective_w'])} first-30-min average input).",
                f"- **30 minutes at the wall buys:** ~{fmt_num(round(best['wh_per_30'], 1))} Wh"
                f" (~{best['pct_per_30']:.0f}% of the pack). Early charging is faster than this"
                " average; the tail end tapers.",
                f"- **Cable gotcha:** {bank.get('cable_gotcha', 'none recorded')}",
                "",
            ]
        )
    return sections


def main() -> int:
    banks = load_json("powerbanks.json")["powerbanks"]
    bricks = load_json("bricks.json")["bricks"]
    cables = load_json("cables.json")["cables"]
    devices = load_json("devices.json")["devices"]

    input_rows = build_input_rows(banks, bricks, cables)
    device_rows = build_device_rows(devices, bricks, banks, cables)
    measurements = load_measurements()
    targets = charge_targets(banks, devices)
    multiport_rows = build_multiport_rows(bricks, targets, measurements)

    if not input_rows:
        print("No viable brick -> bank pairings found; check the data files.", file=sys.stderr)
        return 1
    if not device_rows:
        print("No viable source -> device pairings found; check the data files.", file=sys.stderr)
        return 1

    doc = [
        "# Match Sheet",
        "",
        "Generated by `scripts/match.py`; edit the JSON in `data/`, never this file.",
        "",
        "## Bottom line",
        "",
    ]
    for section in render_bottom_line(banks, input_rows):
        doc.extend(section)

    doc.extend(["## Bottom line for devices", ""])
    for section in render_device_bottom_line(devices, device_rows):
        doc.extend(section)

    doc.extend(
        [
            "## Recharging the banks (wall -> bank)",
            "",
            "Ranked by negotiated watts. `Est. full` includes conversion losses and",
            "end-of-charge taper; `Wh / 30 min` is the airport-layover number.",
            "",
            render_input_table(input_rows),
            "",
            "## Charging devices (wall or bank -> device)",
            "",
            "Single-port use; per-port power drops when multiple devices share a brick or bank.",
            "",
            render_device_table(device_rows),
            "",
            "## Multi-port allocation",
            "",
            "Shared-budget bricks do not change the negotiated protocol - a sink keeps",
            "its 20V contract - they change the current granted at that voltage. Per the",
            "USB-IF Shared Capacity Charger rules, whenever a contract is made or changed",
            "on any port the source recomputes the remaining shared power and re-sends",
            "Source_Capabilities; sinks re-Request without a Hard Reset.",
            "",
            "Official per-combination caps:",
            "",
            render_allocation_combos(bricks),
            "",
            "First-order two-USB-C-port scenarios (model assumes an adequate cable;",
            "measured values from `data/measurements.json` always win over the model):",
            "",
            render_multiport_table(multiport_rows),
            "",
            "Multi-port gotchas:",
            "",
            "- USB-C1 + USB-A on the Anker 747 collapses to 24W total on the label",
            "  (5V/4.8A); one metered run actually exceeded it (88.6W + 20.6W via",
            "  dynamic allocation), but the guaranteed 100W + 22.5W split needs the",
            "  laptop on USB-C2 or C3 whenever USB-A is in use.",
            "- No fixed C+C split is published; PowerIQ 4.0 allocates dynamically. Two",
            "  high-draw laptops balance near 75W + 75W, but a high-draw primary can",
            "  starve the second port: the Y270 measured ~25W behind a charging MacBook",
            "  where the model estimates 54W. Treat the second port behind a charging",
            "  laptop as a ~25-50W slot.",
            "- The Y270 has no documented dual-input (treat as unsupported), slows its",
            "  own recharge during pass-through, and one staff review reports all",
            "  outputs dropping to 5V when multiple output ports are used (unverified).",
            "",
        ]
    )

    OUTPUT_FILE.write_text("\n".join(doc), encoding="utf-8")
    best = input_rows[0]
    print(f"Wrote {OUTPUT_FILE} with {len(input_rows)} bank-input and {len(device_rows)} device pairings.")
    print(
        f"Best combo: {best['brick']['label']} {best['brick_port']['label']} -> "
        f"{best['bank_port']['label']} via {best['cable']['label']} = {fmt_watts(best['watts'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
