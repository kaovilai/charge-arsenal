#!/usr/bin/env python3
"""Compute optimal charging pairings for the charge-arsenal database.

Reads data/{powerbanks,bricks,cables}.json and regenerates MATCHES.md with:
  1. The best brick port -> bank port + cable combo for recharging each bank.
  2. The best port + cable combo for drawing power out of each bank.

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
    return f"{fmt_num(value)}W"


def negotiate_input(brick_port: dict, bank_port: dict, cable: dict) -> dict | None:
    """Best brick-to-bank deal: highest shared PD profile the cable can carry."""
    bank_input = bank_port.get("input")
    brick_output = brick_port.get("output")
    if not bank_input or not brick_output:
        return None
    if brick_port.get("type") != "usb-c" or bank_port.get("type") != "usb-c":
        return None
    if cable.get("connector") != "usb-c":
        return None

    best = None
    for wanted in bank_input.get("pd_profiles", []):
        rail_v = wanted["v"]
        for offered in brick_output.get("pd_profiles", []):
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
                brick_output.get("max_w", float("inf")),
                bank_input.get("max_w", float("inf")),
            )
            better = best is None or watts > best["watts"] or (
                watts == best["watts"] and rail_v > best["profile"]["v"]
            )
            if better:
                best = {"watts": watts, "profile": {"v": rail_v, "a": current}}
    return best


def negotiate_output(bank_port: dict, cable: dict) -> dict | None:
    """Best bank-to-device deal for a USB-C port and cable."""
    bank_output = bank_port.get("output")
    if not bank_output:
        return None
    if bank_port.get("type") != "usb-c" or cable.get("connector") != "usb-c":
        return None

    best = None
    for profile in bank_output.get("pd_profiles", []):
        rail_v = profile["v"]
        if cable.get("max_v", 0) < rail_v:
            continue
        current = min(profile["a"], cable.get("max_a", float("inf")))
        if current <= 0:
            continue
        watts = min(rail_v * current, bank_output.get("max_w", float("inf")))
        better = best is None or watts > best["watts"] or (
            watts == best["watts"] and rail_v > best["profile"]["v"]
        )
        if better:
            best = {"watts": watts, "profile": {"v": rail_v, "a": current}}
    return best


def charge_estimate(bank: dict, watts: float) -> dict:
    behavior = bank["charge_behavior"]
    capacity_wh = bank["capacity_wh"]
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
                        deal = negotiate_input(brick_port, bank_port, cable)
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


def build_output_rows(banks: list, cables: list) -> list:
    rows = []
    for bank in banks:
        for bank_port in bank["ports"]:
            if bank_port.get("type") != "usb-c":
                continue
            for cable in cables:
                deal = negotiate_output(bank_port, cable)
                if not deal:
                    continue
                rows.append(
                    {
                        "bank": bank,
                        "bank_port": bank_port,
                        "cable": cable,
                        "watts": deal["watts"],
                        "profile": deal["profile"],
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


def render_output_table(rows: list) -> str:
    lines = [
        "| Bank port | Cable | Max out | Profile |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {port} | {cable} | {watts} | {profile} |".format(
                port=row["bank_port"]["label"],
                cable=row["cable"]["label"],
                watts=fmt_watts(row["watts"]),
                profile=fmt_profile(row["profile"]),
            )
        )
    return "\n".join(lines)


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
                f" ({fmt_num(best['effective_w'])}W average effective input).",
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

    input_rows = build_input_rows(banks, bricks, cables)
    output_rows = build_output_rows(banks, cables)

    if not input_rows:
        print("No viable brick -> bank pairings found; check the data files.", file=sys.stderr)
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

    doc.extend(
        [
            "## Recharging the banks (wall -> bank)",
            "",
            "Ranked by negotiated watts. `Est. full` includes conversion losses and",
            "end-of-charge taper; `Wh / 30 min` is the airport-layover number.",
            "",
            render_input_table(input_rows),
            "",
            "## Charging devices (bank -> device)",
            "",
            "Single-port use; per-port power drops when multiple devices share the bank.",
            "",
            render_output_table(output_rows),
            "",
        ]
    )

    OUTPUT_FILE.write_text("\n".join(doc), encoding="utf-8")
    best = input_rows[0]
    print(f"Wrote {OUTPUT_FILE} with {len(input_rows)} input and {len(output_rows)} output pairings.")
    print(
        f"Best combo: {best['brick']['label']} {best['brick_port']['label']} -> "
        f"{best['bank_port']['label']} via {best['cable']['label']} = {fmt_watts(best['watts'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
