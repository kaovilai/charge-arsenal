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


def negotiate(source_port: dict, sink_port: dict, cable: dict) -> dict | None:
    """Best source-to-sink deal: highest shared PD profile the cable can carry."""
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
    return best


def charge_estimate(target: dict, watts: float) -> dict:
    behavior = target["charge_behavior"]
    capacity_wh = target["capacity_wh"]
    efficiency = behavior.get("avg_input_efficiency", 0.85)
    effective_w = watts * efficiency
    hours = capacity_wh / effective_w if effective_w > 0 else None
    wh_per_30 = effective_w * 0.5
    pct_per_30 = min(100.0, 100.0 * wh_per_30 / capacity_wh)
    return {
        "effective_w": effective_w,
        "hours": hours,
        "wh_per_30": wh_per_30,
        "pct_per_30": pct_per_30,
    }


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
                    f" ({fmt_watts(best_wall['effective_w'])} average effective input).",
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
                f" ({fmt_watts(best['effective_w'])} average effective input).",
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
