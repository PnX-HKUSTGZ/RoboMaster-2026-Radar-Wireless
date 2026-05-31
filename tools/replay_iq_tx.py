#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gnuradio import blocks, gr, iio

from wireless_link.pluto_ensm import ensure_pluto_ensm_fdd
from wireless_link.protocol import INFO_FREQUENCY_HZ, INFO_RF_BANDWIDTH_HZ, JAM_PROFILES, TEAM_BLUE, TEAM_RED


class IqReplayTx(gr.top_block):
    def __init__(
        self,
        *,
        iq_path: Path,
        uri: str,
        center_freq_hz: int,
        sample_rate: int,
        rf_bandwidth_hz: int,
        attenuation_db: float,
        repeat: bool,
        amplitude: float,
    ) -> None:
        super().__init__("rm2026_wireless_iq_replay_tx")
        self.src = blocks.file_source(gr.sizeof_gr_complex, str(iq_path), repeat)
        self.scale = blocks.multiply_const_cc(float(amplitude))
        self.sink = iio.fmcomms2_sink_fc32(uri, [True, True], 32768, False)
        self.sink.set_len_tag_key("")
        self.sink.set_frequency(float(center_freq_hz))
        self.sink.set_samplerate(int(sample_rate))
        self.sink.set_bandwidth(int(rf_bandwidth_hz))
        self.sink.set_attenuation(0, float(attenuation_db))
        self.sink.set_filter_params("Auto", "", 0, 0)
        self.connect(self.src, self.scale, self.sink)


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _default_meta_path(iq_path: Path) -> Path:
    return iq_path.with_suffix(".json")


def _infer_stage(path: Path, metadata: dict[str, Any]) -> str:
    stage = str(metadata.get("rx_stage") or metadata.get("stage") or "").strip().lower()
    if stage:
        return stage
    name = path.name.lower()
    if name.startswith("info_"):
        return "info"
    if name.startswith("jam1_"):
        return "jam1"
    if name.startswith("jam2_"):
        return "jam2"
    if name.startswith("jam3_"):
        return "jam3"
    return ""


def _infer_level(stage: str, metadata: dict[str, Any], explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    if "jam_level" in metadata:
        return int(metadata["jam_level"])
    if stage.startswith("jam") and len(stage) >= 4 and stage[3:].isdigit():
        return int(stage[3:])
    return 3


def _resolve_radio_params(args: argparse.Namespace, metadata: dict[str, Any]) -> tuple[int, int, int]:
    if args.center_freq_hz:
        center_freq_hz = int(args.center_freq_hz)
    elif metadata.get("center_freq_hz"):
        center_freq_hz = int(metadata["center_freq_hz"])
    else:
        stage = _infer_stage(args.iq, metadata)
        level = _infer_level(stage, metadata, args.jam_level)
        if stage == "info":
            center_freq_hz = INFO_FREQUENCY_HZ[args.team]
        else:
            center_freq_hz = int(JAM_PROFILES[args.team][level]["center_freq_hz"])

    sample_rate = int(args.sample_rate or metadata.get("sample_rate") or 2_083_333)

    if args.rf_bandwidth_hz:
        rf_bandwidth_hz = int(args.rf_bandwidth_hz)
    elif metadata.get("rf_bandwidth_hz"):
        rf_bandwidth_hz = int(metadata["rf_bandwidth_hz"])
    else:
        stage = _infer_stage(args.iq, metadata)
        level = _infer_level(stage, metadata, args.jam_level)
        if stage == "info":
            rf_bandwidth_hz = INFO_RF_BANDWIDTH_HZ
        else:
            rf_bandwidth_hz = int(JAM_PROFILES[args.team][level]["rf_bandwidth_hz"])

    return center_freq_hz, sample_rate, rf_bandwidth_hz


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay recorded RM2026 wireless IQ through Pluto TX. "
            "Use shielded/cabled low-power tests only."
        )
    )
    parser.add_argument("iq", type=Path, help="Recorded complex64 IQ file, usually *.c64.")
    parser.add_argument("--meta", type=Path, default=None, help="Metadata JSON. Defaults to IQ path with .json suffix.")
    parser.add_argument("--tx-uri", default="ip:192.168.2.1", help="Pluto TX URI.")
    parser.add_argument("--team", choices=(TEAM_RED, TEAM_BLUE), default=TEAM_BLUE)
    parser.add_argument("--jam-level", type=int, choices=(1, 2, 3), default=None)
    parser.add_argument("--center-freq-hz", type=int, default=0, help="Override TX center frequency.")
    parser.add_argument("--sample-rate", type=int, default=0, help="Override TX sample rate.")
    parser.add_argument("--rf-bandwidth-hz", type=int, default=0, help="Override TX RF bandwidth.")
    parser.add_argument(
        "--attenuation-db",
        type=float,
        default=89.75,
        help="AD936x TX attenuation. Higher is weaker; keep high unless using coax attenuators.",
    )
    parser.add_argument("--amplitude", type=float, default=0.2, help="IQ amplitude multiplier before TX.")
    parser.add_argument("--repeat", action="store_true", help="Loop the IQ file continuously.")
    parser.add_argument("--duration-s", type=float, default=0.0, help="Stop after this many seconds. 0 waits for EOF/Ctrl-C.")
    parser.add_argument("--dry-run", action="store_true", help="Print resolved TX parameters without transmitting.")
    parser.add_argument("--tx-enable", action="store_true", help="Required to actually transmit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.iq.exists() or args.iq.is_dir():
        raise SystemExit(f"IQ file not found: {args.iq}")

    meta_path = args.meta or _default_meta_path(args.iq)
    metadata = _load_json(meta_path)
    center_freq_hz, sample_rate, rf_bandwidth_hz = _resolve_radio_params(args, metadata)
    seconds = args.iq.stat().st_size / 8.0 / float(sample_rate)
    resolved = {
        "iq": str(args.iq),
        "meta": str(meta_path) if meta_path.exists() else None,
        "tx_uri": args.tx_uri,
        "team": args.team,
        "stage": _infer_stage(args.iq, metadata),
        "jam_level": _infer_level(_infer_stage(args.iq, metadata), metadata, args.jam_level),
        "center_freq_hz": center_freq_hz,
        "sample_rate": sample_rate,
        "rf_bandwidth_hz": rf_bandwidth_hz,
        "attenuation_db": args.attenuation_db,
        "amplitude": args.amplitude,
        "repeat": bool(args.repeat),
        "duration_s": args.duration_s,
        "iq_seconds": seconds,
    }
    print(json.dumps(resolved, ensure_ascii=False, indent=2), flush=True)

    if args.dry_run or not args.tx_enable:
        if not args.tx_enable:
            print("TX disabled. Add --tx-enable to transmit.", file=sys.stderr)
        return 0

    stop = {"value": False}
    signal.signal(signal.SIGINT, lambda *_args: stop.__setitem__("value", True))
    signal.signal(signal.SIGTERM, lambda *_args: stop.__setitem__("value", True))

    ensm_status = ensure_pluto_ensm_fdd(args.tx_uri)
    print(json.dumps({"event": "tx_start", "ensm_status": ensm_status}, ensure_ascii=False), flush=True)
    flowgraph = IqReplayTx(
        iq_path=args.iq,
        uri=args.tx_uri,
        center_freq_hz=center_freq_hz,
        sample_rate=sample_rate,
        rf_bandwidth_hz=rf_bandwidth_hz,
        attenuation_db=args.attenuation_db,
        repeat=bool(args.repeat),
        amplitude=float(args.amplitude),
    )
    flowgraph.start()
    started = time.monotonic()
    try:
        while not stop["value"]:
            if args.duration_s > 0 and time.monotonic() - started >= args.duration_s:
                break
            if not args.repeat and args.duration_s <= 0 and time.monotonic() - started >= seconds + 0.25:
                break
            time.sleep(0.1)
    finally:
        flowgraph.stop()
        flowgraph.wait()
    print(json.dumps({"event": "tx_stop", "elapsed_s": round(time.monotonic() - started, 3)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
