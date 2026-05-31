#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wireless_link.field_recording import parse_field_recording
from wireless_link.protocol import AIR_BIT_ORDER_AUTO, AIR_BIT_ORDER_CLI_CHOICES, TEAM_BLUE, TEAM_RED, profile_for
from wireless_link.sequential_receiver import write_status


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


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
    return "info"


def _mode_level(stage: str, metadata: dict[str, Any]) -> tuple[str, int]:
    if stage == "info":
        return "info", 3
    if stage.startswith("jam") and stage[3:].isdigit():
        return "jam", int(stage[3:])
    mode = str(metadata.get("rx_mode") or "info")
    level = int(metadata.get("jam_level") or 3)
    return mode, level


def _recording_files(input_path: Path, pattern: str) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    files = sorted(input_path.glob(pattern))
    return [path for path in files if path.is_file() and path.suffix == ".c64"]


def _info_positions(decoded: dict[str, Any]) -> tuple[dict[str, list[int]], dict[str, str]]:
    best = decoded.get("best") if "best" in decoded else decoded
    frames = best.get("frames", []) if isinstance(best, dict) else []
    partial_frames = best.get("partial_referee_frames", []) if isinstance(best, dict) else []

    positions: dict[str, list[int]] = {}
    sources: dict[str, str] = {}

    def add_positions(decoded_payload: object, source: str) -> None:
        if not isinstance(decoded_payload, dict):
            return
        for name, item in decoded_payload.items():
            if not isinstance(item, dict):
                continue
            if "x_cm" not in item or "y_cm" not in item:
                continue
            x_val = int(item.get("x_cm", 0) or 0)
            y_val = int(item.get("y_cm", 0) or 0)
            if x_val == 0 and y_val == 0:
                continue
            positions[name] = [x_val, y_val]
            sources[name] = source

    for frame in frames:
        if frame.get("cmd_id") == "0x0A01":
            add_positions(frame.get("decoded"), "full")
    if positions:
        return positions, sources

    for frame in partial_frames:
        if frame.get("cmd_id") == "0x0A01" and bool(frame.get("plausible")):
            add_positions(frame.get("decoded"), "partial")
    return positions, sources


def _frame_counts(frames: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(frame.get("cmd_id")) for frame in frames if frame.get("cmd_id")).items()))


def _build_status(
    *,
    loop_index: int,
    team: str,
    iq_path: Path,
    metadata: dict[str, Any],
    parsed: dict[str, Any],
    tick_s: float,
) -> dict[str, Any]:
    stage = _infer_stage(iq_path, metadata)
    mode, jam_level = _mode_level(stage, metadata)
    best = parsed["decode"]["best"]
    frames = list(best.get("frames", []))
    profile = profile_for(team, mode, jam_level=jam_level)
    positions, position_sources = _info_positions(parsed["decode"])
    frame_counts = _frame_counts(frames)
    partial_frames = list(best.get("partial_referee_frames", []))
    partial_counts = dict(best.get("partial_frame_counts", {}))
    plausible_partial_counts = dict(best.get("plausible_partial_frame_counts", {}))
    sample_rate = float(parsed.get("sample_rate") or metadata.get("sample_rate") or 2_083_333)
    now = time.time()
    recovered = best.get("recovered_jam_key")
    recovered_key = recovered.get("recovered_key") if isinstance(recovered, dict) else None
    strict_key = None
    for frame in frames:
        if frame.get("cmd_id") == "0x0A06":
            decoded_payload = frame.get("decoded")
            if isinstance(decoded_payload, dict):
                strict_key = decoded_payload.get("jam_key")
                break
    return {
        "timestamp_s": now,
        "loop_index": loop_index,
        "team": team,
        "manual_stage": stage,
        "referee_online": False,
        "referee_backend": "offline_recording",
        "referee_error": None,
        "referee_jam_level": jam_level,
        "referee_jam_level_raw": jam_level,
        "referee_can_update_key": False,
        "referee_double_vulnerability_chances": None,
        "referee_double_vulnerability_active": None,
        "referee_state_age_s": 0.0,
        "referee_state_fresh": True,
        "rx_stage": stage,
        "rx_mode": mode,
        "effective_window_s": tick_s,
        "jam_level": jam_level,
        "rx_center_freq_hz": int(metadata.get("center_freq_hz") or profile["center_freq_hz"]),
        "rf_bandwidth_hz": int(metadata.get("rf_bandwidth_hz") or profile["rf_bandwidth_hz"]),
        "sensitivity": float(profile["sensitivity"]),
        "demod_bits": int(parsed.get("demod_bits", 0)),
        "air_packets_found": int(best.get("air_packets_found", 0)),
        "air_packet_payloads": [],
        "referee_frames_found": int(best.get("referee_frames_found", 0)),
        "frame_counts": frame_counts,
        "info_frame_counts": frame_counts if mode == "info" else {},
        "info_positions_cm": positions if mode == "info" else {},
        "info_positions_source": position_sources if mode == "info" else {},
        "selected_air_bit_order": best.get("selected_air_bit_order") or parsed["decode"].get("recommended_air_bit_order"),
        "bit_polarity": best.get("bit_polarity") or parsed["decode"].get("recommended_bit_polarity"),
        "strict_key": strict_key,
        "recovered_key": recovered_key,
        "recovered_key_detail": recovered,
        "recovered_key_vote_state": {"ready": False, "reason": "offline_recording_replay"},
        "key_upload_state": {"sent": False, "reason": "offline_recording_replay"},
        "partial_referee_frames_found": int(best.get("partial_referee_frames_found", 0)),
        "partial_frame_counts": partial_counts,
        "plausible_partial_frame_counts": plausible_partial_counts,
        "partial_referee_frames": partial_frames,
        "access_code_diagnostics": None,
        "iq_stats": {},
        "raw_fm_stats": {},
        "demod_stats": {},
        "recording": {
            "enabled": True,
            "replay": True,
            "iq_path": str(iq_path),
            "metadata_path": str(iq_path.with_suffix(".json")) if iq_path.with_suffix(".json").exists() else "",
            "sample_rate": sample_rate,
            "seconds": float(parsed.get("seconds") or 0.0),
        },
        "stage_decision": {
            "stage": stage,
            "mode": mode,
            "jam_level": jam_level,
            "source": "offline_recording_replay",
            "referee_level": jam_level,
            "referee_level_raw": jam_level,
            "referee_state_age_s": 0.0,
            "referee_state_fresh": True,
            "post_rx_stage": stage,
            "post_rx_referee_level": jam_level,
            "post_rx_referee_level_raw": jam_level,
            "post_rx_referee_state_age_s": 0.0,
            "post_rx_referee_state_fresh": True,
        },
        "rx_error": None,
        "last_error": None,
    }


def _append_jsonl(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay recorded wireless IQ files as live status/wireless.json updates without SDR."
    )
    parser.add_argument("--input", type=Path, required=True, help="A .c64 file or directory containing .c64 recordings.")
    parser.add_argument("--pattern", default="info_*.c64", help="Glob pattern when --input is a directory.")
    parser.add_argument("--status-out", type=Path, required=True, help="Output wireless.json path.")
    parser.add_argument("--jsonl-out", type=Path, default=None, help="Optional per-window status log.")
    parser.add_argument("--team", choices=(TEAM_RED, TEAM_BLUE), default=TEAM_BLUE)
    parser.add_argument("--tick-s", type=float, default=1.0, help="Wall-clock interval between replayed windows.")
    parser.add_argument("--loops", type=int, default=1, help="Directory replay loops. 0 means repeat forever.")
    parser.add_argument("--sample-rate", type=float, default=0.0, help="Override sample rate. 0 uses metadata/default.")
    parser.add_argument("--air-bit-order", choices=AIR_BIT_ORDER_CLI_CHOICES, default=AIR_BIT_ORDER_AUTO)
    parser.add_argument("--rx-preprocess", choices=("off", "dc_block"), default="off")
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--gain-mu", type=float, default=0.175)
    parser.add_argument("--omega-relative-limit", type=float, default=0.005)
    parser.add_argument("--symbol-freq-error", type=float, default=0.0048)
    parser.add_argument("--once", action="store_true", help="Replay exactly one pass and exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    files = _recording_files(args.input, args.pattern)
    if not files:
        raise SystemExit(f"no .c64 files found: {args.input} pattern={args.pattern}")

    stop = {"value": False}
    signal.signal(signal.SIGINT, lambda *_args: stop.__setitem__("value", True))
    signal.signal(signal.SIGTERM, lambda *_args: stop.__setitem__("value", True))

    loop_index = 0
    replay_round = 0
    while not stop["value"]:
        for iq_path in files:
            if stop["value"]:
                break
            started = time.monotonic()
            metadata = _load_json(iq_path.with_suffix(".json"))
            stage = _infer_stage(iq_path, metadata)
            mode, jam_level = _mode_level(stage, metadata)
            sample_rate = float(args.sample_rate or metadata.get("sample_rate") or 2_083_333)
            try:
                parsed = parse_field_recording(
                    iq_in=iq_path,
                    mode=mode,
                    team=args.team,
                    jam_level=jam_level,
                    sample_rate=sample_rate,
                    air_bit_order=args.air_bit_order,
                    rx_preprocess=args.rx_preprocess,
                    max_samples=args.max_samples,
                    gain_mu=args.gain_mu,
                    omega_relative_limit=args.omega_relative_limit,
                    symbol_freq_error=args.symbol_freq_error,
                )
                status = _build_status(
                    loop_index=loop_index,
                    team=args.team,
                    iq_path=iq_path,
                    metadata=metadata,
                    parsed=parsed,
                    tick_s=float(args.tick_s),
                )
            except Exception as exc:
                status = {
                    "timestamp_s": time.time(),
                    "loop_index": loop_index,
                    "team": args.team,
                    "manual_stage": stage,
                    "referee_online": False,
                    "referee_backend": "offline_recording",
                    "referee_error": str(exc),
                    "rx_stage": stage,
                    "rx_mode": mode,
                    "jam_level": jam_level,
                    "demod_bits": 0,
                    "air_packets_found": 0,
                    "referee_frames_found": 0,
                    "frame_counts": {},
                    "info_frame_counts": {},
                    "info_positions_cm": {},
                    "info_positions_source": {},
                    "rx_error": str(exc),
                    "last_error": str(exc),
                    "recording": {"enabled": True, "replay": True, "iq_path": str(iq_path)},
                }
            write_status(args.status_out, status)
            _append_jsonl(args.jsonl_out, status)
            print(
                json.dumps(
                    {
                        "loop_index": loop_index,
                        "iq": str(iq_path),
                        "rx_stage": status.get("rx_stage"),
                        "air_packets_found": status.get("air_packets_found"),
                        "referee_frames_found": status.get("referee_frames_found"),
                        "info_frame_counts": status.get("info_frame_counts"),
                        "info_positions_cm": status.get("info_positions_cm"),
                        "rx_error": status.get("rx_error"),
                        "status_out": str(args.status_out),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            loop_index += 1
            elapsed = time.monotonic() - started
            sleep_s = max(0.0, float(args.tick_s) - elapsed)
            if sleep_s > 0:
                time.sleep(sleep_s)
        replay_round += 1
        if args.once or args.input.is_file():
            break
        if args.loops > 0 and replay_round >= args.loops:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
