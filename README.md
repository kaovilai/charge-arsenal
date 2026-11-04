# Charge Arsenal

Personal database of power banks, wall bricks, and cables, plus a matcher that
answers one question: **which port do I plug into which, to move the most watts
in the least time?**

The goal is airport-layover math: minimum minutes stuck at a wall outlet,
maximum watt-hours back into the pack.

## Quick start

```sh
python3 scripts/match.py
```

This regenerates [MATCHES.md](MATCHES.md) with every brick x bank x cable
pairing, ranked by negotiated watts, plus estimated time-to-full and
watt-hours recovered per 30-minute wall session.

## Current arsenal

| Device | Role | Key numbers |
| --- | --- | --- |
| Raydyn Y270 (Shenzhen Yonglian Tech) | 27000mAh / 99.3Wh power bank | 65W max input, 140W max single-port output, 250W total output |
| CUKTECH 10 | 10000mAh / 37Wh power bank | 90W max input, 100W USB-C PD output, 150W total output |
| Anker 747 Charger (GaNPrime 150W, A2340) | 4-port wall brick | 100W max per USB-C port, 150W total shared output |
| CUKTECH A12 65W GaN ultrathin card charger | 1-port wall brick | 65W max on USB-C |

## Fastest routes

### Wall -> Raydyn Y270

- **Best port:** any USB-C port on the Anker 747 or the CUKTECH A12, into
  either USB-C port on the Y270.
- **Best speed:** 65W (20V/3.25A). The Y270 itself is the ceiling; a 100W or
  150W brick cannot charge it faster.
- **Cable:** the Y270’s included 100W cable, the CUKTECH A12’s included 100W
  cable, or the CUKTECH 10’s included 240W cable all work. A basic 3A cable
  drops this to about 60W.

### Wall -> CUKTECH 10

- **Best port:** any USB-C port on the Anker 747, into the CUKTECH 10’s USB-C
  port.
- **Best speed:** 90W (20V/4.5A). The Anker’s USB-C port can supply 100W, but
  the bank caps input at 90W.
- **Cable:** use the CUKTECH 10’s included 240W/6A cable. The CUKTECH A12
  card charger tops out at 65W and cannot saturate this bank.

### Raydyn Y270 -> device

- **Best port:** use either USB-C port by itself.
- **Best speed:** 140W (28V/5A) with a 240W/EPR-rated USB-C cable, such as the
  CUKTECH 10’s included cable.
- **Cable gotcha:** the Y270’s included 100W cable limits output to 100W
  (20V/5A), not 140W.

### CUKTECH 10 -> device

- **Best port:** use the USB-C port by itself.
- **Best speed:** 100W standard PD (20V/5A) with the included 240W/6A cable.
- **Proprietary exception:** compatible Xiaomi devices can negotiate up to
  120W via Xiaomi’s proprietary fast-charge protocol, not standard USB-C PD.

### Charging both banks at once

The Anker 747 can use two USB-C ports at once, but they share a 150W total
ceiling. In practice, that is enough to run the Y270 at its 65W input cap and
still have headroom for the CUKTECH 10, though the exact split depends on
negotiation and cable quality.

## Device cheat sheets

### Raydyn Y270

- 27000mAh / 99.3Wh
- 2x USB-C bidirectional PD 3.1, 1x USB-A output-only
- 65W max input (20V/3.25A)
- 140W max single USB-C output (28V/5A)
- 250W total output across all ports
- Included cable is 100W / 20V / 5A, so it caps output at 100W
- Claimed 0-to-full is about 2.5h at 65W

### CUKTECH 10

- 10000mAh / 37Wh
- 1x USB-C bidirectional, 1x USB-A output
- 90W max input (20V/4.5A)
- 100W max USB-C standard PD output
- 120W max via Xiaomi proprietary fast charge on compatible devices
- 33W max USB-A output
- 150W total output
- Included cable is 240W / 6A USB-C, PD 3.1 capable
- Claimed 55% charge in 15 minutes at 90W

### Anker 747 Charger (A2340)

- 3x USB-C, 1x USB-A
- 100W max on any USB-C port when used alone
- 22.5W max on USB-A
- 150W total shared output
- GaNPrime, PowerIQ 4.0, ActiveShield 2.0
- Input voltage: 100-240V

### CUKTECH A12 65W GaN ultrathin card charger

- 1x USB-C
- 65W max (20V/3.25A)
- PD 3.0 and PPS
- Foldable plug
- Includes a 100W USB-C cable

## Data model

All data lives in `data/` as plain JSON:

- `powerbanks.json` - the banks themselves
- `bricks.json` - wall chargers
- `cables.json` - the wires that quietly decide everything

Each USB-C port carries separate `input` and `output` blocks with a `max_w`
ceiling and a list of `pd_profiles` (voltage/current pairs). The matcher picks
the highest shared profile that the cable can physically carry, so a weak cable
or a missing 20V rail shows up as a lower negotiated wattage instead of a
mystery.

## Adding gear

1. Copy an existing entry in the relevant `data/*.json` file.
2. Fill in real numbers from the device label, manual, or a USB power meter.
3. Run `python3 scripts/match.py` and commit the regenerated `MATCHES.md`.

## Data confidence

Every device entry carries a `data_confidence` field:

- `researched-listings` - numbers taken from manufacturer/listing pages, not
  independently verified.
- `measured` - verified with a USB power meter. Gold standard; not yet done.

## Roadmap

- Measure actual PD profiles and taper curves with a USB power meter
- Model charge speed vs. state of charge (the last 20% is always slower)
- Model multi-port wall-brick allocation
- Static viewer for GitHub Pages
