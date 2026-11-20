#!/usr/bin/env python3
"""Generate the 30-minute, two-AC-plug charge strategy sheet."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_FILE = ROOT / "STRATEGY.md"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from match import charge_estimate, soc_gain  # noqa: E402

SOC_STEPS = [0, 20, 40, 60, 80, 90]


def load_json(name: str) -> dict:
    with (DATA_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def find_item(items: list, item_id: str) -> dict:
    for item in items:
        if item["id"] == item_id:
            return item
    raise ValueError(f"Missing item with id {item_id}")


def best_wall_watts(device: dict, bricks: list) -> float:
    """Best single-port wall wattage for a device, ignoring cable limits."""
    device_max = device.get("charge_behavior", {}).get("max_input_w", 0)
    if not device_max:
        return 0.0

    best = 0.0
    for brick in bricks:
        for port in brick.get("ports", []):
            if port.get("type") != "usb-c":
                continue
            output = port.get("output", {})
            port_max = output.get("max_w", 0)
            best = max(best, min(device_max, port_max))
    return best


def wall_watts_from_bricks(device: dict, bricks: list, brick_ids: list[str]) -> float:
    """Best wall wattage for a device from the named bricks."""
    device_max = device.get("charge_behavior", {}).get("max_input_w", 0)
    if not device_max:
        return 0.0

    best = 0.0
    for brick_id in brick_ids:
        brick = find_item(bricks, brick_id)
        for port in brick.get("ports", []):
            if port.get("type") != "usb-c":
                continue
            output = port.get("output", {})
            port_max = output.get("max_w", 0)
            best = max(best, min(device_max, port_max))
    return best


def gain_wh(device: dict, soc: int, watts: float) -> float:
    """30-minute gain from a given state of charge.

    SOC-aware: uses the measured taper curves so a nearly-full battery gains
    less than an empty one. Flat first-order estimate when no curve exists.
    """
    tapered = soc_gain(device, soc, watts)
    if tapered:
        return tapered["wh"]
    capacity_wh = device.get("capacity_wh", 0)
    efficiency = device.get("charge_behavior", {}).get("avg_input_efficiency", 0.85)
    remaining_wh = capacity_wh * (1 - soc / 100)
    return min(watts * efficiency * 0.5, remaining_wh)


def fmt_wh(value: float) -> str:
    return f"{value:.1f} Wh"


def build_matrix(macbook: dict, cuktech: dict, bricks: list) -> list[list[str]]:
    anker_watts = {
        "macbook": wall_watts_from_bricks(macbook, bricks, ["anker-747-a2340"]),
        "cuktech": wall_watts_from_bricks(cuktech, bricks, ["anker-747-a2340"]),
    }
    a12_watts = {
        "macbook": wall_watts_from_bricks(macbook, bricks, ["cuktech-ac65b2-65w"]),
        "cuktech": wall_watts_from_bricks(cuktech, bricks, ["cuktech-ac65b2-65w"]),
    }

    rows = []
    for mac_soc in SOC_STEPS:
        row = [f"{mac_soc}%"]
        for cuk_soc in SOC_STEPS:
            mac_on_anker = gain_wh(macbook, mac_soc, anker_watts["macbook"])
            cuk_on_a12 = gain_wh(cuktech, cuk_soc, a12_watts["cuktech"])
            cuk_on_anker = gain_wh(cuktech, cuk_soc, anker_watts["cuktech"])
            mac_on_a12 = gain_wh(macbook, mac_soc, a12_watts["macbook"])

            option_a = mac_on_anker + cuk_on_a12
            option_b = cuk_on_anker + mac_on_a12
            if option_a >= option_b:
                row.append(f"MacBook / {fmt_wh(option_a)}")
            else:
                row.append(f"CUKTECH 10 / {fmt_wh(option_b)}")
        rows.append(row)
    return rows


def build_a12_matrix(macbook: dict, cuktech: dict, bricks: list) -> list[list[str]]:
    """Which device should get the CUKTECH AC65B2 if it is the only brick in use?"""
    a12_watts = {
        "macbook": wall_watts_from_bricks(macbook, bricks, ["cuktech-ac65b2-65w"]),
        "cuktech": wall_watts_from_bricks(cuktech, bricks, ["cuktech-ac65b2-65w"]),
    }

    rows = []
    for mac_soc in SOC_STEPS:
        row = [f"{mac_soc}%"]
        for cuk_soc in SOC_STEPS:
            mac_on_a12 = gain_wh(macbook, mac_soc, a12_watts["macbook"])
            cuk_on_a12 = gain_wh(cuktech, cuk_soc, a12_watts["cuktech"])
            if mac_on_a12 >= cuk_on_a12:
                row.append(f"MacBook / {fmt_wh(mac_on_a12)}")
            else:
                row.append(f"CUKTECH 10 / {fmt_wh(cuk_on_a12)}")
        rows.append(row)
    return rows


def render_matrix(rows: list[list[str]]) -> str:
    header = ["MacBook SOC"] + [f"CUKTECH 10: {soc}%" for soc in SOC_STEPS]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join([" --- "] * len(header)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render_gain_table(devices: list, bricks: list) -> str:
    lines = [
        "| Device | Best wall input | 30-minute gain from 0% |",
        "| --- | ---: | ---: |",
    ]
    for device in devices:
        watts = best_wall_watts(device, bricks)
        est = charge_estimate(device, watts)
        label = f"{device['brand']} {device['model']}"
        lines.append(f"| {label} | {watts:g}W | {fmt_wh(est['wh_per_30'])} |")
    return "\n".join(lines)


def main() -> int:
    devices = load_json("devices.json")["devices"]
    banks = load_json("powerbanks.json")["powerbanks"]
    bricks = load_json("bricks.json")["bricks"]

    macbook = find_item(devices, "macbook-pro-14-m4-pro")
    cuktech = find_item(banks, "cuktech-10")

    matrix_rows = build_matrix(macbook, cuktech, bricks)
    a12_matrix_rows = build_a12_matrix(macbook, cuktech, bricks)
    all_charge_targets = devices + banks

    doc = [
        "# 30-minute charge strategy",
        "",
        "Generated by `scripts/strategy.py`; edit the JSON in `data/`, never this file.",
        "",
        "Assumes two AC wall plugs: one Anker 747 Charger and one CUKTECH AC65B2.",
        "The matrix shows which device should get the Anker 747; the other device",
        "uses the CUKTECH AC65B2. Gains are state-of-charge aware where measured taper",
        "curves exist (the last 20% charges much slower), flat first-order otherwise.",
        "",
        "## Who gets the Anker 747?",
        "",
        render_matrix(matrix_rows),
        "",
        "## Who gets the CUKTECH AC65B2?",
        "",
        "Use this when the AC65B2 is the only charger in play, or when you are deciding",
        "which device should take the AC65B2 while the Anker 747 feeds the other port.",
        "",
        render_matrix(a12_matrix_rows),
        "",
        "## 30-minute gain from 0% state of charge",
        "",
        render_gain_table(all_charge_targets, bricks),
        "",
        "## Rule of thumb",
        "",
        "- If the MacBook is below about 40%, put it on the Anker 747.",
        "- If the MacBook is above about 60%, give the Anker to the CUKTECH 10 - unless the CUKTECH 10 is itself nearly full (80%+), where the MacBook still wins.",
        "- The taper is the reason the boundary moved: the same device at 90% SOC gains roughly a quarter of what it gains at 0%, so a nearly-full battery rarely deserves the big port.",
        "- Put the Z Fold7, iPad, or Y270 on the CUKTECH AC65B2 or the Anker 747's second USB-C port - but expect far less than the single-port number on that second port (see below).",
        "- The Galaxy Watch8 is modeled as a 10W USB-C-fed target through Samsung's official wireless charger.",
        "- Samsung Super Fast Charging: the Z Fold7 needs PPS 3.3-11V at >=2.75A for its ~22W full speed. The Anker 747, CUKTECH AC65B2, and CUKTECH 10 all meter PPS covering that; the Y270's PPS is undocumented, so Fold7-on-Y270 is modeled at the conservative 15W fallback. Plain non-PPS 9V charging is disputed (~15-22W depending on source).",
        "",
        "## Multi-port reality check",
        "",
        "The Anker 747 publishes no fixed split for two USB-C ports in use (150W",
        "shared, dynamic PowerIQ 4.0 allocation; two laptops balance near 75W + 75W).",
        "A high-draw primary can leave the second port far below the first-order",
        "model: the Y270 measured ~25W at 20V on the second port while the MacBook",
        "charged on the first, versus a 54W model estimate. Treat the second USB-C",
        "port behind a charging laptop as a ~25-50W slot, and prefer the CUKTECH AC65B2",
        "(~60W measured to the same Y270) when the bank is the priority.",
        "",
    ]

    OUTPUT_FILE.write_text("\n".join(doc), encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
