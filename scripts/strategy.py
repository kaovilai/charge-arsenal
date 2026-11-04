#!/usr/bin/env python3
"""Generate the 30-minute, two-AC-plug charge strategy sheet."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_FILE = ROOT / "STRATEGY.md"

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
    """First-order 30-minute gain, capped by remaining capacity."""
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
        "macbook": wall_watts_from_bricks(macbook, bricks, ["cuktech-a12-65w-card"]),
        "cuktech": wall_watts_from_bricks(cuktech, bricks, ["cuktech-a12-65w-card"]),
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
    """Which device should get the CUKTECH A12 if it is the only brick in use?"""
    a12_watts = {
        "macbook": wall_watts_from_bricks(macbook, bricks, ["cuktech-a12-65w-card"]),
        "cuktech": wall_watts_from_bricks(cuktech, bricks, ["cuktech-a12-65w-card"]),
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
        gain = gain_wh(device, 0, watts)
        label = f"{device['brand']} {device['model']}"
        lines.append(f"| {label} | {watts:g}W | {fmt_wh(gain)} |")
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
        "Assumes two AC wall plugs: one Anker 747 Charger and one CUKTECH A12.",
        "The matrix shows which device should get the Anker 747; the other device",
        "uses the CUKTECH A12. Numbers are first-order estimates and do not yet",
        "model charge taper near full.",
        "",
        "## Who gets the Anker 747?",
        "",
        render_matrix(matrix_rows),
        "",
        "## Who gets the CUKTECH A12?",
        "",
        "Use this when the A12 is the only charger in play, or when you are deciding",
        "which device should take the A12 while the Anker 747 feeds the other port.",
        "",
        render_matrix(a12_matrix_rows),
        "",
        "## 30-minute gain from 0% state of charge",
        "",
        render_gain_table(all_charge_targets, bricks),
        "",
        "## Rule of thumb",
        "",
        "- If the MacBook is below about 60%, put it on the Anker 747.",
        "- If the MacBook is above 60% and the CUKTECH 10 is below about 20%, put the CUKTECH 10 on the Anker 747.",
        "- Otherwise, the MacBook still wins the Anker 747.",
        "- Put the Z Fold7, iPad, or Y270 on the CUKTECH A12 or the Anker 747's second USB-C port.",
        "- The Galaxy Watch8 is modeled as a 10W USB-C-fed target through Samsung's official wireless charger.",
        "",
    ]

    OUTPUT_FILE.write_text("\n".join(doc), encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
