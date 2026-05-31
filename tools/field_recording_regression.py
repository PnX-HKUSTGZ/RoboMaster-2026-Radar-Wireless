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

from wireless_link.field_recording import field_regression


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Regression check for RM2026 real field recordings.")
    parser.add_argument(
        "--iq-in",
        type=Path,
        default=Path("recordings/data/20260530_182117/wireless/info_20260530_182247_loop000023.c64"),
    )
    parser.add_argument(
        "--l2-iq-in",
        type=Path,
        default=Path("recordings/data/20260530_182117/wireless/jam2_20260530_182343_loop000002.c64"),
    )
    parser.add_argument("--expected-key", default="fcYqTC")
    parser.add_argument("--min-frames", type=int, default=1)
    parser.add_argument("--json-out", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = field_regression(
        l3_iq_in=args.iq_in,
        l2_iq_in=args.l2_iq_in,
        expected_key=args.expected_key,
        min_frames=args.min_frames,
    )
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
