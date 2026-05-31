#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wireless_link.protocol import AIR_BIT_ORDER_CLI_CHOICES, DEFAULT_AIR_BIT_ORDER, TEAM_BLUE, TEAM_RED
from wireless_link.referee_link import DEFAULT_REFEREE_BAUD, DEFAULT_REFEREE_PORT
from wireless_link.sequential_receiver import (
    DECODE_MODE_CHOICES,
    DECODE_MODE_EXACT,
    FUZZY_ACCESS_MAX_HAMMING,
    JAM_KEY_PRINTABLE_MIN_VOTES,
    STAGE_CHOICES,
    SequentialWirelessReceiver,
    WirelessLinkConfig,
    write_status,
)


_UNSET = object()


def _load_yaml_config(path: str | Path) -> dict[str, Any]:
    if not str(path).strip():
        return {}
    config_path = Path(path)
    if not config_path.exists() or config_path.is_dir():
        return {}
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _cfg(config: dict[str, Any], key: str, default: Any) -> Any:
    return config.get(key, default)


def _arg_value(value: Any, config: dict[str, Any], key: str, default: Any) -> Any:
    return _cfg(config, key, default) if value is _UNSET else value


def _bool_arg_value(value: bool, config: dict[str, Any], key: str, default: bool) -> bool:
    return bool(_cfg(config, key, default)) if value is False else True


def _log_event(event: str, **payload: Any) -> None:
    print(
        json.dumps(
            {
                "timestamp_s": time.time(),
                "event": event,
                **payload,
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sequential single-center-frequency RM2026 wireless-link RX.")
    parser.add_argument("--config", default="", help="YAML config for standalone wireless RX defaults.")
    parser.add_argument("--team", choices=(TEAM_RED, TEAM_BLUE), default=_UNSET)
    parser.add_argument("--rx-uri", default=_UNSET)
    parser.add_argument("--referee-port", default=_UNSET)
    parser.add_argument("--referee-baud", type=int, default=_UNSET)
    parser.add_argument("--referee-source", choices=("direct", "gateway", "none"), default=_UNSET)
    parser.add_argument("--gateway-state-path", default=_UNSET)
    parser.add_argument("--gateway-tx-requests-path", default=_UNSET)
    parser.add_argument("--manual-stage", choices=STAGE_CHOICES, default=_UNSET)
    parser.add_argument("--no-referee", action="store_true")
    parser.add_argument("--allow-recovered-key-upload", action="store_true")
    parser.add_argument("--window-s", type=float, default=_UNSET)
    parser.add_argument("--info-window-s", type=float, default=_UNSET)
    parser.add_argument("--loops", type=int, default=_UNSET, help="0 means run until interrupted")
    parser.add_argument("--gain-mode", choices=("manual", "slow_attack", "fast_attack", "hybrid"), default=_UNSET)
    parser.add_argument("--manual-gain", type=float, default=_UNSET)
    parser.add_argument("--freq-offset-hz", type=float, default=_UNSET)
    parser.add_argument("--gain-mu", type=float, default=_UNSET)
    parser.add_argument("--omega-relative-limit", type=float, default=_UNSET)
    parser.add_argument("--symbol-freq-error", type=float, default=_UNSET)
    parser.add_argument("--dc-blocker-length", type=int, default=_UNSET)
    parser.add_argument("--probe-decim", type=int, default=_UNSET)
    parser.add_argument("--recent-decode-bits", type=int, default=_UNSET)
    parser.add_argument("--air-bit-order", choices=AIR_BIT_ORDER_CLI_CHOICES, default=_UNSET)
    parser.add_argument("--decode-mode", choices=DECODE_MODE_CHOICES, default=_UNSET)
    parser.add_argument("--fuzzy-access-max-hamming", type=int, default=_UNSET)
    parser.add_argument("--jam-key-min-votes", type=int, default=_UNSET)
    parser.add_argument("--recovered-key-min-candidates", type=int, default=_UNSET)
    parser.add_argument("--recovered-key-min-confidence", type=float, default=_UNSET)
    parser.add_argument("--recovered-key-vote-windows", type=int, default=_UNSET)
    parser.add_argument("--referee-state-max-age-s", type=float, default=_UNSET)
    parser.add_argument("--status-out", type=Path, default=_UNSET)
    parser.add_argument("--record-iq-dir", default=_UNSET, help="Optional directory for per-window raw Pluto IQ .c64 recordings.")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    file_config = _load_yaml_config(args.config)
    recording_config = file_config.get("recording", {})
    if not isinstance(recording_config, dict):
        recording_config = {}
    record_iq_dir = _arg_value(args.record_iq_dir, file_config, "record_iq_dir", None)
    if record_iq_dir is None:
        record_iq_dir = str(recording_config.get("dir", "")) if bool(recording_config.get("enabled", False)) else ""
    stop = {"value": False}
    signal.signal(signal.SIGINT, lambda *_args: stop.__setitem__("value", True))
    signal.signal(signal.SIGTERM, lambda *_args: stop.__setitem__("value", True))
    config = WirelessLinkConfig(
        team=str(_arg_value(args.team, file_config, "team", TEAM_BLUE)),
        rx_uri=str(_arg_value(args.rx_uri, file_config, "rx_uri", "ip:192.168.2.10")),
        referee_port=str(_arg_value(args.referee_port, file_config, "referee_port", DEFAULT_REFEREE_PORT)),
        referee_baud=int(_arg_value(args.referee_baud, file_config, "referee_baud", DEFAULT_REFEREE_BAUD)),
        referee_source=str(_arg_value(args.referee_source, file_config, "referee_source", "direct")),
        gateway_state_path=str(_arg_value(args.gateway_state_path, file_config, "gateway_state_path", "")),
        gateway_tx_requests_path=str(_arg_value(args.gateway_tx_requests_path, file_config, "gateway_tx_requests_path", "")),
        manual_stage=_arg_value(args.manual_stage, file_config, "manual_stage", None),
        no_referee=_bool_arg_value(args.no_referee, file_config, "no_referee", False),
        allow_recovered_key_upload=_bool_arg_value(
            args.allow_recovered_key_upload,
            file_config,
            "allow_recovered_key_upload",
            False,
        ),
        window_s=float(_arg_value(args.window_s, file_config, "window_s", 1.5)),
        info_window_s=float(_arg_value(args.info_window_s, file_config, "info_window_s", 1.0)),
        gain_mode=str(_arg_value(args.gain_mode, file_config, "gain_mode", "manual")),
        manual_gain=float(_arg_value(args.manual_gain, file_config, "manual_gain", 35.0)),
        freq_offset_hz=float(_arg_value(args.freq_offset_hz, file_config, "freq_offset_hz", 0.0)),
        gain_mu=float(_arg_value(args.gain_mu, file_config, "gain_mu", 0.175)),
        omega_relative_limit=float(_arg_value(args.omega_relative_limit, file_config, "omega_relative_limit", 0.005)),
        symbol_freq_error=float(_arg_value(args.symbol_freq_error, file_config, "symbol_freq_error", 0.0048)),
        dc_blocker_length=int(_arg_value(args.dc_blocker_length, file_config, "dc_blocker_length", 0)),
        probe_decim=int(_arg_value(args.probe_decim, file_config, "probe_decim", 100)),
        recent_decode_bits=int(_arg_value(args.recent_decode_bits, file_config, "recent_decode_bits", 240_000)),
        air_bit_order=str(_arg_value(args.air_bit_order, file_config, "air_bit_order", DEFAULT_AIR_BIT_ORDER)),
        decode_mode=str(_arg_value(args.decode_mode, file_config, "decode_mode", DECODE_MODE_EXACT)),
        fuzzy_access_max_hamming=int(
            _arg_value(args.fuzzy_access_max_hamming, file_config, "fuzzy_access_max_hamming", FUZZY_ACCESS_MAX_HAMMING)
        ),
        jam_key_min_votes=int(_arg_value(args.jam_key_min_votes, file_config, "jam_key_min_votes", JAM_KEY_PRINTABLE_MIN_VOTES)),
        recovered_key_min_candidates=int(
            _arg_value(args.recovered_key_min_candidates, file_config, "recovered_key_min_candidates", 2)
        ),
        recovered_key_min_confidence=float(
            _arg_value(args.recovered_key_min_confidence, file_config, "recovered_key_min_confidence", 0.6)
        ),
        recovered_key_vote_windows=int(_arg_value(args.recovered_key_vote_windows, file_config, "recovered_key_vote_windows", 4)),
        record_iq_dir=str(record_iq_dir or ""),
        referee_state_max_age_s=float(_arg_value(args.referee_state_max_age_s, file_config, "referee_state_max_age_s", 3.0)),
    )
    loops = int(_arg_value(args.loops, file_config, "loops", 0))
    status_out = Path(_arg_value(args.status_out, file_config, "status_out", "/tmp/rm2026_wireless_status.json"))
    quiet = _bool_arg_value(args.quiet, file_config, "quiet", False)
    with SequentialWirelessReceiver(config) as receiver:
        while not stop["value"] and (loops <= 0 or receiver.loop_index < loops):
            loop_index = receiver.loop_index
            _log_event("wireless_step_start", loop_index=loop_index)
            step_start_s = time.monotonic()
            status = receiver.step()
            write_status(status_out, status)
            _log_event(
                "wireless_step_done",
                loop_index=loop_index,
                elapsed_s=round(time.monotonic() - step_start_s, 3),
                rx_stage=status.rx_stage,
                air_packets_found=status.air_packets_found,
                referee_frames_found=status.referee_frames_found,
                strict_key=status.strict_key,
                recovered_key=status.recovered_key,
                rx_error=status.rx_error,
                stage_decision=status.stage_decision,
                status_out=str(status_out),
            )
            if not quiet:
                print(status.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
