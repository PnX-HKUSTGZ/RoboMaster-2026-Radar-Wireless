#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wireless_link.field_recording import parse_field_recording
from wireless_link.protocol import AIR_BIT_ORDER_AUTO, AIR_BIT_ORDER_CLI_CHOICES, TEAM_BLUE, TEAM_RED


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse GNU Radio complex64 RM2026 field recordings.")
    parser.add_argument("--iq-in", type=Path, required=True)
    parser.add_argument("--mode", choices=("info", "jam"), default="jam")
    parser.add_argument("--team", choices=(TEAM_RED, TEAM_BLUE), default=TEAM_BLUE)
    parser.add_argument("--jam-level", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--sample-rate", type=float, default=2_000_000.0)
    parser.add_argument("--air-bit-order", choices=AIR_BIT_ORDER_CLI_CHOICES, default=AIR_BIT_ORDER_AUTO)
    parser.add_argument("--rx-preprocess", choices=("off", "dc_block"), default="off")
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--gain-mu", type=float, default=0.175)
    parser.add_argument("--omega-relative-limit", type=float, default=0.005)
    parser.add_argument("--symbol-freq-error", type=float, default=0.0048)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--max-print-frames", type=int, default=12)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = parse_field_recording(
        iq_in=args.iq_in,
        mode=args.mode,
        team=args.team,
        jam_level=args.jam_level,
        sample_rate=args.sample_rate,
        air_bit_order=args.air_bit_order,
        rx_preprocess=args.rx_preprocess,
        max_samples=args.max_samples,
        gain_mu=args.gain_mu,
        omega_relative_limit=args.omega_relative_limit,
        symbol_freq_error=args.symbol_freq_error,
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    printed = dict(output)
    printed["decode"] = dict(output["decode"])
    printed["decode"]["best"] = dict(output["decode"]["best"])
    printed["decode"]["best"]["frames"] = output["decode"]["best"].get("frames", [])[: max(0, args.max_print_frames)]
    printed["decode"]["best"]["frames_printed"] = len(printed["decode"]["best"]["frames"])
    print(json.dumps(printed, ensure_ascii=False, indent=2))
    best = output["decode"]["best"]
    decoded_frame_count = int(best.get("referee_frames_found", 0))
    partial_frame_count = int(best.get("partial_referee_frames_found", 0))
    return 0 if decoded_frame_count > 0 or partial_frame_count > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
