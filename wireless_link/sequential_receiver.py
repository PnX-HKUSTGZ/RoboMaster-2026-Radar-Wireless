from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .decoder import (
    DEFAULT_RECENT_DECODE_BITS,
    DECODE_MODE_EXACT,
    DECODE_MODE_CHOICES,
    FUZZY_ACCESS_MAX_HAMMING,
    JAM_KEY_PRINTABLE_MIN_VOTES,
    decode_recent_bits,
    jam_key_candidates_from_packets,
    recover_jam_key_from_candidates,
    recover_air_packets_from_bits_fuzzy_access,
    recover_jam_key_from_packets,
    strict_jam_key,
)
from .pluto_rx import PLUTO_SAMPLE_RATE, PlutoWirelessRx
from .protocol import (
    AIR_BIT_ORDER_AUTO,
    DEFAULT_AIR_BIT_ORDER,
    JAM_ACCESS_CODE,
    TEAM_BLUE,
    TEAM_RED,
    profile_for,
    recover_air_packets_from_bits,
)
from .referee_link import (
    BLUE_RADAR_ROBOT_ID,
    DEFAULT_REFEREE_BAUD,
    DEFAULT_REFEREE_PORT,
    KEY_UPLOAD_THROTTLE_S,
    RED_RADAR_ROBOT_ID,
    RefereeLink,
    radar_info_to_dict,
)

STAGE_JAM1 = "jam1"
STAGE_JAM2 = "jam2"
STAGE_JAM3 = "jam3"
STAGE_INFO = "info"
STAGE_CHOICES = (STAGE_JAM1, STAGE_JAM2, STAGE_JAM3, STAGE_INFO)


@dataclass
class WirelessLinkConfig:
    team: str = TEAM_BLUE
    rx_uri: str = "ip:192.168.2.10"
    referee_port: str = DEFAULT_REFEREE_PORT
    referee_baud: int = DEFAULT_REFEREE_BAUD
    referee_source: str = "direct"
    gateway_state_path: str = ""
    gateway_tx_requests_path: str = ""
    manual_stage: str | None = None
    no_referee: bool = False
    allow_recovered_key_upload: bool = False
    window_s: float = 1.5
    info_window_s: float = 1.0
    gain_mode: str = "manual"
    manual_gain: float = 35.0
    freq_offset_hz: float = 0.0
    gain_mu: float = 0.175
    omega_relative_limit: float = 0.005
    symbol_freq_error: float = 0.0048
    dc_blocker_length: int = 0
    probe_decim: int = 100
    recent_decode_bits: int = DEFAULT_RECENT_DECODE_BITS
    air_bit_order: str = DEFAULT_AIR_BIT_ORDER
    decode_mode: str = DECODE_MODE_EXACT
    fuzzy_access_max_hamming: int = FUZZY_ACCESS_MAX_HAMMING
    jam_key_min_votes: int = JAM_KEY_PRINTABLE_MIN_VOTES
    recovered_key_min_candidates: int = 2
    recovered_key_min_confidence: float = 0.6
    recovered_key_vote_windows: int = 4
    record_iq_dir: str = ""
    referee_state_max_age_s: float = 3.0


@dataclass
class WirelessLinkStatus:
    timestamp_s: float
    loop_index: int
    team: str
    manual_stage: str | None
    referee_online: bool
    referee_backend: str | None
    referee_error: str | None
    referee_jam_level: int | None
    referee_jam_level_raw: int | None
    referee_can_update_key: bool | None
    referee_double_vulnerability_chances: int | None
    referee_double_vulnerability_active: bool | None
    referee_state_age_s: float | None
    referee_state_fresh: bool
    rx_stage: str
    rx_mode: str
    effective_window_s: float
    dc_blocker_length: int
    jam_level: int
    rx_center_freq_hz: int
    rf_bandwidth_hz: int
    sensitivity: float
    demod_bits: int
    air_packets_found: int
    air_packet_payloads: list[dict[str, Any]]
    referee_frames_found: int
    frame_counts: dict[str, int]
    info_frame_counts: dict[str, int]
    info_positions_cm: dict[str, list[int]]
    info_positions_source: dict[str, str]
    info_economics: dict[str, Any]
    info_remaining_bullets: dict[str, Any]
    selected_air_bit_order: str | None
    bit_polarity: str | None
    strict_key: str | None
    recovered_key: str | None
    recovered_key_detail: dict[str, Any] | None
    recovered_key_vote_state: dict[str, Any]
    key_upload_state: dict[str, Any]
    partial_referee_frames_found: int = 0
    partial_frame_counts: dict[str, int] = field(default_factory=dict)
    plausible_partial_frame_counts: dict[str, int] = field(default_factory=dict)
    partial_referee_frames: list[dict[str, Any]] = field(default_factory=list)
    access_code_diagnostics: dict[str, Any] | None = None
    iq_stats: dict[str, Any] = field(default_factory=dict)
    raw_fm_stats: dict[str, Any] = field(default_factory=dict)
    demod_stats: dict[str, Any] = field(default_factory=dict)
    recording: dict[str, Any] = field(default_factory=dict)
    stage_decision: dict[str, Any] = field(default_factory=dict)
    rx_error: str | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def validate_config(config: WirelessLinkConfig) -> None:
    if config.team not in (TEAM_RED, TEAM_BLUE):
        raise ValueError(f"invalid team: {config.team}")
    if config.manual_stage is not None and config.manual_stage not in STAGE_CHOICES:
        raise ValueError(f"invalid manual_stage: {config.manual_stage}")
    if config.referee_source not in ("direct", "gateway", "none"):
        raise ValueError(f"invalid referee_source: {config.referee_source}")
    if config.decode_mode not in DECODE_MODE_CHOICES:
        raise ValueError(f"invalid decode_mode: {config.decode_mode}")
    if config.recovered_key_vote_windows < 1:
        raise ValueError("recovered_key_vote_windows must be >= 1")


def stage_to_mode_level(stage: str) -> tuple[str, int]:
    if stage == STAGE_INFO:
        return "info", 3
    if stage.startswith("jam"):
        return "jam", int(stage[-1])
    raise ValueError(f"invalid stage: {stage}")


def stage_from_referee_level(level: int | None) -> str:
    if level == 1:
        return STAGE_JAM1
    if level == 2:
        return STAGE_JAM2
    if level == 3:
        return STAGE_INFO
    return STAGE_JAM1


class SequentialWirelessReceiver:
    def __init__(self, config: WirelessLinkConfig) -> None:
        validate_config(config)
        self.config = config
        self.referee: RefereeLink | None = None
        self.gateway_info: dict[str, Any] | None = None
        self.key_upload_state: dict[str, float] = {}
        self.logged_jam_decode_levels: set[int] = set()
        self.loop_index = 0
        self.last_error: str | None = None
        self.gateway_state_mtime_s: float | None = None
        self.recovered_vote_stage: str | None = None
        self.recovered_vote_windows: deque[dict[str, Any]] = deque()

    def open(self) -> None:
        if self.config.no_referee or self.config.referee_source in ("gateway", "none"):
            return
        self.referee = RefereeLink(port=self.config.referee_port, baud=self.config.referee_baud)
        try:
            self.referee.open()
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            if self.config.manual_stage:
                self.referee = None
                return
            raise

    def close(self) -> None:
        if self.referee is not None:
            self.referee.close()

    def __enter__(self) -> "SequentialWirelessReceiver":
        self.open()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def poll_referee(self) -> None:
        if self.config.referee_source == "gateway":
            self._poll_gateway_state()
        elif self.referee is not None:
            self.referee.poll()

    def _poll_gateway_state(self) -> None:
        path = Path(self.config.gateway_state_path) if self.config.gateway_state_path else None
        if path is None or not path.exists():
            return
        try:
            self.gateway_info = json.loads(path.read_text(encoding="utf-8"))
            self.gateway_state_mtime_s = path.stat().st_mtime
            self.last_error = None
        except Exception as exc:
            self.last_error = f"gateway_state_read_failed:{exc}"

    def referee_state_age_s(self) -> float | None:
        if self.config.referee_source == "gateway":
            state_time = None
            if self.gateway_info:
                radar_info = self.gateway_info.get("radar_info")
                if isinstance(radar_info, dict):
                    state_time = radar_info.get("timestamp_s")
                if state_time is None:
                    state_time = self.gateway_info.get("timestamp_s")
            if state_time is None:
                state_time = self.gateway_state_mtime_s
            if state_time is None:
                return None
            return max(0.0, time.time() - float(state_time))
        return 0.0 if self.referee is not None and self.referee.latest_radar_info is not None else None

    def referee_state_fresh(self) -> bool:
        age = self.referee_state_age_s()
        return age is not None and age <= self.config.referee_state_max_age_s

    def _gateway_radar_info_dict(self) -> dict[str, Any]:
        if not self.gateway_info:
            return {
                "referee_jam_level": None,
                "referee_jam_level_raw": None,
                "referee_can_update_key": None,
                "referee_double_vulnerability_chances": None,
                "referee_double_vulnerability_active": None,
            }
        radar_info = self.gateway_info.get("radar_info") or {}
        return {
            "referee_jam_level": radar_info.get("jam_level"),
            "referee_jam_level_raw": radar_info.get("jam_level_raw"),
            "referee_can_update_key": radar_info.get("can_update_key"),
            "referee_double_vulnerability_chances": radar_info.get("double_vulnerability_chances"),
            "referee_double_vulnerability_active": radar_info.get("double_vulnerability_active"),
        }

    def current_stage(self) -> str:
        return self.stage_decision()["stage"]

    def stage_decision(self) -> dict[str, Any]:
        if self.config.manual_stage:
            mode, jam_level = stage_to_mode_level(self.config.manual_stage)
            return {
                "stage": self.config.manual_stage,
                "mode": mode,
                "jam_level": jam_level,
                "source": "manual_stage",
                "referee_level": None,
                "referee_level_raw": None,
                "referee_state_age_s": self.referee_state_age_s(),
                "referee_state_fresh": self.referee_state_fresh(),
            }
        age = self.referee_state_age_s()
        fresh = self.referee_state_fresh()
        if not fresh:
            return {
                "stage": STAGE_JAM1,
                "mode": "jam",
                "jam_level": 1,
                "source": "default_stale_or_missing_referee_state",
                "referee_level": None,
                "referee_level_raw": None,
                "referee_state_age_s": age,
                "referee_state_fresh": False,
            }
        if self.config.referee_source == "gateway":
            radar_info = self.gateway_info.get("radar_info") if self.gateway_info else None
            level = radar_info.get("jam_level") if isinstance(radar_info, dict) else None
            raw_level = radar_info.get("jam_level_raw") if isinstance(radar_info, dict) else None
        else:
            radar_info = self.referee.latest_radar_info if self.referee is not None else None
            level = radar_info.jam_level if radar_info else None
            raw_level = radar_info.jam_level_raw if radar_info else None
        stage = stage_from_referee_level(level)
        mode, jam_level = stage_to_mode_level(stage)
        return {
            "stage": stage,
            "mode": mode,
            "jam_level": jam_level,
            "source": "referee_radar_info",
            "referee_level": level,
            "referee_level_raw": raw_level,
            "referee_state_age_s": age,
            "referee_state_fresh": True,
        }

    def _recording_paths(self, stage: str) -> tuple[Path | None, Path | None]:
        if not self.config.record_iq_dir:
            return None, None
        output_dir = Path(self.config.record_iq_dir)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        stem = f"{stage}_{timestamp}_loop{self.loop_index:06d}"
        return output_dir / f"{stem}.c64", output_dir / f"{stem}.json"

    def _decode_results_path(self) -> Path | None:
        if not self.config.record_iq_dir:
            return None
        return Path(self.config.record_iq_dir) / "decode_results.jsonl"

    def _write_recording_metadata(self, meta_path: Path | None, payload: dict[str, Any]) -> None:
        if meta_path is None:
            return
        try:
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self.last_error = f"recording_metadata_write_failed:{exc}"

    def _append_decode_result(self, payload: dict[str, Any]) -> None:
        path = self._decode_results_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as exc:
            self.last_error = f"decode_result_write_failed:{exc}"

    def _maybe_log_decode_result(
        self,
        *,
        stage: str,
        mode: str,
        jam_level: int,
        decoded: dict[str, Any],
        rx_result: dict[str, Any],
        iq_path: Path | None,
    ) -> None:
        base_payload = {
            "timestamp_s": time.time(),
            "team": self.config.team,
            "loop_index": self.loop_index,
            "rx_stage": stage,
            "rx_mode": mode,
            "jam_level": jam_level,
            "center_freq_hz": int(rx_result.get("rx_center_freq_hz", 0)),
            "rf_bandwidth_hz": int(rx_result.get("rf_bandwidth_hz", 0)),
            "sample_rate": PLUTO_SAMPLE_RATE,
            "window_s": float(self.config.window_s),
            "effective_window_s": float(rx_result.get("effective_window_s", self.config.window_s)),
            "iq_path": str(iq_path) if iq_path is not None else "",
            "demod_bits": int(rx_result.get("demod_bits", 0)),
            "air_packets_found": int(rx_result.get("air_packets_found", 0)),
            "referee_frames_found": int(rx_result.get("referee_frames_found", 0)),
            "frame_counts": dict(rx_result.get("frame_counts", {})),
            "partial_referee_frames_found": int(rx_result.get("partial_referee_frames_found", 0)),
            "partial_frame_counts": dict(rx_result.get("partial_frame_counts", {})),
            "plausible_partial_frame_counts": dict(rx_result.get("plausible_partial_frame_counts", {})),
            "selected_air_bit_order": rx_result.get("selected_air_bit_order"),
            "bit_polarity": rx_result.get("bit_polarity"),
        }

        if mode == "jam" and jam_level in (1, 2):
            key = rx_result.get("strict_key")
            key_source = "strict"
            if not key:
                key = rx_result.get("recovered_key")
                key_source = rx_result.get("recovered_key_detail", {}).get("source", "recovered")
            if not key or jam_level in self.logged_jam_decode_levels:
                return
            recovered_detail = rx_result.get("recovered_key_detail")
            self._append_decode_result(
                {
                    **base_payload,
                    "event": "jam_key_decoded_once",
                    "key": key,
                    "key_source": key_source,
                    "recovered_key_detail": recovered_detail,
                    "frames": decoded.get("frames", []),
                }
            )
            self.logged_jam_decode_levels.add(jam_level)
            return

        if mode == "info" and int(rx_result.get("referee_frames_found", 0)) > 0:
            self._append_decode_result(
                {
                    **base_payload,
                    "event": "info_frames_decoded",
                    "info_frame_counts": dict(rx_result.get("info_frame_counts", {})),
                    "info_positions_cm": dict(rx_result.get("info_positions_cm", {})),
                    "info_positions_source": dict(rx_result.get("info_positions_source", {})),
                    "info_economics": dict(rx_result.get("info_economics", {})),
                    "info_remaining_bullets": dict(rx_result.get("info_remaining_bullets", {})),
                    "frames": decoded.get("frames", []),
                }
            )
            return

        if mode == "info" and int(rx_result.get("partial_referee_frames_found", 0)) > 0:
            partial_frames = list(rx_result.get("partial_referee_frames", []))
            self._append_decode_result(
                {
                    **base_payload,
                    "event": "info_partial_referee_frames_decoded",
                    "partial_referee_frames_found": int(rx_result.get("partial_referee_frames_found", 0)),
                    "partial_frame_counts": dict(rx_result.get("partial_frame_counts", {})),
                    "plausible_partial_frame_counts": dict(rx_result.get("plausible_partial_frame_counts", {})),
                    "info_positions_cm": dict(rx_result.get("info_positions_cm", {})),
                    "info_positions_source": dict(rx_result.get("info_positions_source", {})),
                    "info_economics": dict(rx_result.get("info_economics", {})),
                    "info_remaining_bullets": dict(rx_result.get("info_remaining_bullets", {})),
                    "partial_referee_frames": partial_frames,
                    "plausible_partial_referee_frames": [
                        frame for frame in partial_frames if bool(frame.get("plausible"))
                    ],
                }
            )
            return

        if mode == "info" and int(rx_result.get("air_packets_found", 0)) > 0:
            partial_payloads = [
                payload
                for payload in decoded.get("air_packet_payloads", [])
                if int(payload.get("nonzero_count", 0)) > 0
            ]
            if not partial_payloads:
                return
            self._append_decode_result(
                {
                    **base_payload,
                    "event": "info_air_packets_partial",
                    "reason": "air_access_and_header_valid_but_no_full_referee_frame",
                    "partial_packet_count": len(partial_payloads),
                    "air_packet_payloads": partial_payloads,
                }
            )

    def _recording_metadata(
        self,
        iq_path: Path | None,
        stage: str,
        mode: str,
        jam_level: int,
        profile: dict[str, Any],
        rx_result: dict[str, Any],
    ) -> dict[str, Any]:
        radar_info = self._gateway_radar_info_dict() if self.config.referee_source == "gateway" else {}
        referee_jam_level = radar_info.get("referee_jam_level")
        if self.referee is not None and self.referee.latest_radar_info is not None:
            referee_jam_level = self.referee.latest_radar_info.jam_level
        return {
            "timestamp_s": time.time(),
            "team": self.config.team,
            "rx_stage": stage,
            "rx_mode": mode,
            "jam_level": jam_level,
            "center_freq_hz": int(profile["center_freq_hz"] + self.config.freq_offset_hz),
            "sample_rate": PLUTO_SAMPLE_RATE,
            "rf_bandwidth_hz": int(profile["rf_bandwidth_hz"]),
            "window_s": float(self.config.window_s),
            "effective_window_s": float(rx_result.get("effective_window_s", self.config.window_s)),
            "referee_jam_level": referee_jam_level,
            "iq_path": str(iq_path) if iq_path is not None else "",
            "demod_bits": int(rx_result.get("demod_bits", 0)),
            "air_packets_found": int(rx_result.get("air_packets_found", 0)),
            "referee_frames_found": int(rx_result.get("referee_frames_found", 0)),
            "frame_counts": dict(rx_result.get("frame_counts", {})),
            "info_frame_counts": dict(rx_result.get("info_frame_counts", {})),
            "info_positions_cm": dict(rx_result.get("info_positions_cm", {})),
            "info_positions_source": dict(rx_result.get("info_positions_source", {})),
            "info_economics": dict(rx_result.get("info_economics", {})),
            "info_remaining_bullets": dict(rx_result.get("info_remaining_bullets", {})),
            "partial_referee_frames_found": int(rx_result.get("partial_referee_frames_found", 0)),
            "partial_frame_counts": dict(rx_result.get("partial_frame_counts", {})),
            "plausible_partial_frame_counts": dict(rx_result.get("plausible_partial_frame_counts", {})),
            "strict_key": rx_result.get("strict_key"),
            "recovered_key": rx_result.get("recovered_key"),
            "rx_error": rx_result.get("rx_error"),
        }

    def receive_once(self, stage: str) -> dict[str, Any]:
        mode, jam_level = stage_to_mode_level(stage)
        profile = profile_for(self.config.team, mode, jam_level=jam_level)
        iq_path, meta_path = self._recording_paths(stage)
        effective_window_s = float(self.config.info_window_s if mode == "info" else self.config.window_s)
        dc_blocker_length = int(self.config.dc_blocker_length) if mode == "info" else 0
        rx = PlutoWirelessRx(
            uri=self.config.rx_uri,
            center_freq=int(profile["center_freq_hz"] + self.config.freq_offset_hz),
            rf_bandwidth=int(profile["rf_bandwidth_hz"]),
            gain_mode=self.config.gain_mode,
            manual_gain=self.config.manual_gain,
            sensitivity=float(profile["sensitivity"]),
            gain_mu=self.config.gain_mu,
            omega_relative_limit=self.config.omega_relative_limit,
            freq_error=self.config.symbol_freq_error,
            dc_blocker_length=dc_blocker_length,
            probe_decim=self.config.probe_decim,
            record_iq_path=str(iq_path) if iq_path is not None else None,
        )
        rx.start()
        try:
            time.sleep(max(0.05, effective_window_s))
        finally:
            rx.stop()
            rx.wait()
        try:
            bits = rx.bits()
            decoded = decode_recent_bits(
                bits,
                mode=mode,
                recent_limit=self.config.recent_decode_bits,
                air_bit_order=self.config.air_bit_order,
                decode_mode=self.config.decode_mode,
                fuzzy_access_max_hamming=self.config.fuzzy_access_max_hamming,
                jam_key_min_votes=self.config.jam_key_min_votes,
            )
            decode_timestamp_s = time.time()
            info_economics = dict(decoded.get("info_economics", {})) if mode == "info" else {}
            info_remaining_bullets = dict(decoded.get("info_remaining_bullets", {})) if mode == "info" else {}
            if info_economics:
                info_economics["source_timestamp_s"] = decode_timestamp_s
            if info_remaining_bullets:
                info_remaining_bullets["source_timestamp_s"] = decode_timestamp_s
            strict_key = strict_jam_key(decoded) if mode == "jam" else None
            recovered = decoded.get("recovered_jam_key")
            if mode == "jam" and (not recovered or not recovered.get("recovered_key")):
                recovered = self._decode_packets_for_recovery(bits, decoded)
            recovery_candidate_detail = recovered if mode == "jam" and not strict_key else None
            rx_result = {
                "rx_stage": stage,
                "rx_mode": mode,
                "jam_level": jam_level,
                "rx_center_freq_hz": int(profile["center_freq_hz"] + self.config.freq_offset_hz),
                "rf_bandwidth_hz": int(profile["rf_bandwidth_hz"]),
                "sensitivity": float(profile["sensitivity"]),
                "effective_window_s": effective_window_s,
                "dc_blocker_length": dc_blocker_length,
                "demod_bits": int(decoded.get("demod_bits", 0)),
                "air_packets_found": int(decoded.get("air_packets_found", 0)),
                "referee_frames_found": int(decoded.get("referee_frames_found", 0)),
                "frame_counts": dict(decoded.get("frame_counts", {})),
                "info_frame_counts": dict(decoded.get("frame_counts", {})) if mode == "info" else {},
                "info_positions_cm": dict(decoded.get("info_positions_cm", {})) if mode == "info" else {},
                "info_positions_source": dict(decoded.get("info_positions_source", {})) if mode == "info" else {},
                "info_economics": info_economics,
                "info_remaining_bullets": info_remaining_bullets,
                "partial_referee_frames_found": int(decoded.get("partial_referee_frames_found", 0)),
                "partial_frame_counts": dict(decoded.get("partial_frame_counts", {})),
                "plausible_partial_frame_counts": dict(decoded.get("plausible_partial_frame_counts", {})),
                "partial_referee_frames": decoded.get("partial_referee_frames", []),
                "selected_air_bit_order": decoded.get("selected_air_bit_order", self.config.air_bit_order),
                "bit_polarity": decoded.get("bit_polarity"),
                "access_code_diagnostics": decoded.get("access_code_diagnostics"),
                "strict_key": strict_key,
                "recovered_key": None,
                "recovered_key_detail": recovery_candidate_detail,
                "air_packet_payloads": decoded.get("air_packet_payloads", []),
                "iq_stats": rx.iq_stats(),
                "raw_fm_stats": rx.raw_fm_stats(),
                "demod_stats": rx.demod_stats(),
                "recording": {
                    "enabled": iq_path is not None,
                    "iq_path": str(iq_path) if iq_path is not None else "",
                    "metadata_path": str(meta_path) if meta_path is not None else "",
                    "sample_rate": PLUTO_SAMPLE_RATE if iq_path is not None else None,
                },
            }
            if iq_path is not None:
                self._write_recording_metadata(
                    meta_path,
                    self._recording_metadata(iq_path, stage, mode, jam_level, profile, rx_result),
                )
                self._maybe_log_decode_result(
                    stage=stage,
                    mode=mode,
                    jam_level=jam_level,
                    decoded=decoded,
                    rx_result=rx_result,
                    iq_path=iq_path,
                )
            return rx_result
        finally:
            rx.release_resources()

    def _empty_rx_result(self, stage: str, error: str | None = None) -> dict[str, Any]:
        mode, jam_level = stage_to_mode_level(stage)
        profile = profile_for(self.config.team, mode, jam_level=jam_level)
        return {
            "rx_stage": stage,
            "rx_mode": mode,
            "jam_level": jam_level,
            "rx_center_freq_hz": int(profile["center_freq_hz"] + self.config.freq_offset_hz),
            "rf_bandwidth_hz": int(profile["rf_bandwidth_hz"]),
            "sensitivity": float(profile["sensitivity"]),
            "effective_window_s": float(self.config.info_window_s if mode == "info" else self.config.window_s),
            "dc_blocker_length": int(self.config.dc_blocker_length) if mode == "info" else 0,
            "demod_bits": 0,
            "air_packets_found": 0,
            "air_packet_payloads": [],
            "referee_frames_found": 0,
            "frame_counts": {},
            "info_frame_counts": {},
            "info_positions_cm": {},
            "info_positions_source": {},
            "info_economics": {},
            "info_remaining_bullets": {},
            "partial_referee_frames_found": 0,
            "partial_frame_counts": {},
            "plausible_partial_frame_counts": {},
            "partial_referee_frames": [],
            "selected_air_bit_order": self.config.air_bit_order,
            "bit_polarity": None,
            "access_code_diagnostics": None,
            "strict_key": None,
            "recovered_key": None,
            "recovered_key_detail": None,
            "iq_stats": {},
            "raw_fm_stats": {},
            "demod_stats": {},
            "recording": {"enabled": False, "iq_path": "", "metadata_path": "", "sample_rate": None},
            "rx_error": error,
        }

    def _decode_packets_for_recovery(self, bit_items: list[int], result: dict) -> dict | None:
        if result.get("selected_air_bit_order") == AIR_BIT_ORDER_AUTO:
            return None
        bits = [int(item) & 1 for item in bit_items[-DEFAULT_RECENT_DECODE_BITS:]]
        if result.get("bit_polarity") == "inverted":
            bits = [bit ^ 1 for bit in bits]
        air_bit_order = result.get("selected_air_bit_order", DEFAULT_AIR_BIT_ORDER)
        if self.config.decode_mode == "fuzzy_access":
            packets = recover_air_packets_from_bits_fuzzy_access(
                bits,
                access_code=JAM_ACCESS_CODE,
                air_bit_order=air_bit_order,
                max_hamming=self.config.fuzzy_access_max_hamming,
            )
        else:
            packets = recover_air_packets_from_bits(
                bits,
                access_code=JAM_ACCESS_CODE,
                air_bit_order=air_bit_order,
            )
        detail = recover_jam_key_from_packets(packets, min_votes=self.config.jam_key_min_votes)
        detail["candidate_keys_hex"] = [candidate.hex() for candidate in jam_key_candidates_from_packets(packets)]
        return detail

    def _empty_recovered_vote_state(self, stage: str | None = None, reason: str = "not_buffering") -> dict[str, Any]:
        return {
            "stage": stage,
            "buffered_windows": 0,
            "target_windows": max(1, int(self.config.recovered_key_vote_windows)),
            "candidate_count": 0,
            "ready": False,
            "reason": reason,
            "detail": None,
        }

    def _reset_recovered_vote_buffer(self, stage: str | None = None) -> None:
        self.recovered_vote_stage = stage
        self.recovered_vote_windows.clear()

    def _update_recovered_vote_state(self, rx_result: dict[str, Any]) -> dict[str, Any]:
        stage = str(rx_result.get("rx_stage") or "")
        mode = rx_result.get("rx_mode")
        level = int(rx_result.get("jam_level") or 0)
        target_windows = max(1, int(self.config.recovered_key_vote_windows))
        if mode != "jam" or level not in (1, 2):
            self._reset_recovered_vote_buffer(None)
            return self._empty_recovered_vote_state(stage, "not_l1_l2_jam")
        if rx_result.get("strict_key"):
            self._reset_recovered_vote_buffer(stage)
            return self._empty_recovered_vote_state(stage, "strict_key_found_buffer_cleared")
        if self.recovered_vote_stage != stage:
            self._reset_recovered_vote_buffer(stage)

        detail = rx_result.get("recovered_key_detail")
        candidate_keys_hex = []
        if isinstance(detail, dict):
            candidate_keys_hex = [str(item) for item in detail.get("candidate_keys_hex", []) if isinstance(item, str)]
        self.recovered_vote_windows.append(
            {
                "loop_index": self.loop_index,
                "timestamp_s": time.time(),
                "stage": stage,
                "candidate_keys_hex": candidate_keys_hex,
                "air_packets_found": int(rx_result.get("air_packets_found") or 0),
            }
        )
        while len(self.recovered_vote_windows) > target_windows:
            self.recovered_vote_windows.popleft()

        all_candidates: list[bytes] = []
        for window in self.recovered_vote_windows:
            for candidate_hex in window.get("candidate_keys_hex", []):
                try:
                    candidate = bytes.fromhex(str(candidate_hex))
                except ValueError:
                    continue
                if len(candidate) == 6:
                    all_candidates.append(candidate)

        state = {
            "stage": stage,
            "buffered_windows": len(self.recovered_vote_windows),
            "target_windows": target_windows,
            "candidate_count": len(all_candidates),
            "ready": False,
            "reason": "waiting_for_6s_vote_window",
            "window_loop_indices": [window["loop_index"] for window in self.recovered_vote_windows],
            "window_candidate_counts": [
                len(window.get("candidate_keys_hex", [])) for window in self.recovered_vote_windows
            ],
            "detail": None,
        }
        if len(self.recovered_vote_windows) < target_windows:
            return state

        vote_duration_s = target_windows * float(self.config.window_s)
        vote_detail = recover_jam_key_from_candidates(
            all_candidates,
            min_votes=self.config.jam_key_min_votes,
            source=f"recovered_{vote_duration_s:g}s_vote",
        )
        state["ready"] = True
        state["reason"] = "vote_window_ready"
        state["detail"] = vote_detail
        rx_result["recovered_key"] = vote_detail.get("recovered_key")
        rx_result["recovered_key_detail"] = vote_detail
        if (
            vote_detail.get("recovered_key")
            and level not in self.logged_jam_decode_levels
            and self._recovered_key_allowed(rx_result)
        ):
            self._append_decode_result(
                {
                    "timestamp_s": time.time(),
                    "team": self.config.team,
                    "loop_index": self.loop_index,
                    "rx_stage": stage,
                    "rx_mode": mode,
                    "jam_level": level,
                    "event": "jam_key_decoded_once",
                    "key": vote_detail.get("recovered_key"),
                    "key_source": vote_detail.get("source"),
                    "recovered_key_detail": vote_detail,
                    "recovered_key_vote_state": state,
                    "center_freq_hz": int(rx_result.get("rx_center_freq_hz", 0)),
                    "rf_bandwidth_hz": int(rx_result.get("rf_bandwidth_hz", 0)),
                }
            )
            self.logged_jam_decode_levels.add(level)
        return state

    def _recovered_key_allowed(self, rx_result: dict[str, Any]) -> bool:
        detail = rx_result.get("recovered_key_detail")
        if not isinstance(detail, dict):
            return False
        candidates = int(detail.get("candidate_count") or 0)
        confidence = float(detail.get("confidence") or 0.0)
        return (
            candidates >= self.config.recovered_key_min_candidates
            and confidence >= self.config.recovered_key_min_confidence
        )

    def _source_for_recovered_key(self, rx_result: dict[str, Any]) -> str:
        detail = rx_result.get("recovered_key_detail")
        source = detail.get("source") if isinstance(detail, dict) else None
        return str(source or "recovered")

    def maybe_upload_key(self, rx_result: dict[str, Any]) -> dict[str, Any]:
        mode = rx_result.get("rx_mode")
        level = int(rx_result.get("jam_level") or 0)
        if mode != "jam" or level not in (1, 2):
            return {"sent": False, "reason": "not_l1_l2_jam"}
        if self.config.referee_source == "gateway":
            return self._maybe_request_gateway_key_upload(rx_result)
        if self.referee is None:
            return {"sent": False, "reason": "no_referee"}
        info = self.referee.latest_radar_info
        if info is None:
            return {"sent": False, "reason": "no_0x020e"}
        if not self.referee_state_fresh():
            return {"sent": False, "reason": "stale_referee_radar_info"}
        if not info.can_update_key:
            return {"sent": False, "reason": "referee_not_accepting_key"}
        key = rx_result.get("strict_key")
        source = "strict"
        if not key and self.config.allow_recovered_key_upload and self._recovered_key_allowed(rx_result):
            key = rx_result.get("recovered_key")
            source = self._source_for_recovered_key(rx_result)
        if not key:
            return {"sent": False, "reason": "no_key"}
        now = time.monotonic()
        previous = self.key_upload_state.get(key)
        if previous is not None and now - previous < KEY_UPLOAD_THROTTLE_S:
            return {"sent": False, "reason": "local_throttle", "key": key, "source": source}
        result = self.referee.upload_key(key, now=now)
        result["source"] = source
        if result.get("sent"):
            self.key_upload_state[key] = now
            if source != "strict":
                self._reset_recovered_vote_buffer(str(rx_result.get("rx_stage") or ""))
        return result

    def _maybe_request_gateway_key_upload(self, rx_result: dict[str, Any]) -> dict[str, Any]:
        radar_info = self.gateway_info.get("radar_info") if self.gateway_info else None
        if not isinstance(radar_info, dict):
            return {"sent": False, "reason": "no_gateway_radar_info"}
        if not self.referee_state_fresh():
            return {"sent": False, "reason": "stale_gateway_radar_info"}
        if not bool(radar_info.get("can_update_key")):
            return {"sent": False, "reason": "referee_not_accepting_key"}
        key = rx_result.get("strict_key")
        source = "strict"
        if not key and self.config.allow_recovered_key_upload and self._recovered_key_allowed(rx_result):
            key = rx_result.get("recovered_key")
            source = self._source_for_recovered_key(rx_result)
        if not key:
            return {"sent": False, "reason": "no_key"}
        now = time.monotonic()
        previous = self.key_upload_state.get(key)
        if previous is not None and now - previous < KEY_UPLOAD_THROTTLE_S:
            return {"sent": False, "reason": "local_throttle", "key": key, "source": source}
        if not self.config.gateway_tx_requests_path:
            return {"sent": False, "reason": "no_gateway_tx_requests_path", "key": key, "source": source}
        path = Path(self.config.gateway_tx_requests_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        request = {
            "timestamp_s": time.time(),
            "type": "key_upload",
            "key": key,
            "source": source,
            "stage": rx_result.get("rx_stage"),
            "sender_id": BLUE_RADAR_ROBOT_ID if self.config.team == TEAM_BLUE else RED_RADAR_ROBOT_ID,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.key_upload_state[key] = now
        if source != "strict":
            self._reset_recovered_vote_buffer(str(rx_result.get("rx_stage") or ""))
        return {"sent": True, "reason": "queued_gateway_request", "key": key, "source": source}

    def step(self) -> WirelessLinkStatus:
        self.poll_referee()
        radar_info = self.referee.latest_radar_info if self.referee is not None else None
        stage_decision = self.stage_decision()
        stage = str(stage_decision["stage"])
        rx_error = None
        try:
            rx_result = self.receive_once(stage)
            self.last_error = None
        except Exception as exc:
            rx_error = str(exc)
            self.last_error = rx_error
            rx_result = self._empty_rx_result(stage, error=rx_error)
        recovered_vote_state = self._update_recovered_vote_state(rx_result)
        self.poll_referee()
        post_rx_stage_decision = self.stage_decision()
        status_stage_decision = {
            **stage_decision,
            "post_rx_stage": post_rx_stage_decision.get("stage"),
            "post_rx_referee_level": post_rx_stage_decision.get("referee_level"),
            "post_rx_referee_level_raw": post_rx_stage_decision.get("referee_level_raw"),
            "post_rx_referee_state_age_s": post_rx_stage_decision.get("referee_state_age_s"),
            "post_rx_referee_state_fresh": post_rx_stage_decision.get("referee_state_fresh"),
        }
        upload_result = self.maybe_upload_key(rx_result)
        status = WirelessLinkStatus(
            timestamp_s=time.time(),
            loop_index=self.loop_index,
            team=self.config.team,
            manual_stage=self.config.manual_stage,
            referee_online=bool(self.gateway_info) if self.config.referee_source == "gateway" else bool(self.referee and self.referee.online),
            referee_backend="gateway" if self.config.referee_source == "gateway" else (self.referee.serial.backend_name if self.referee else None),
            referee_error=self.last_error if self.config.referee_source == "gateway" else (self.referee.last_error if self.referee else self.last_error),
            **(self._gateway_radar_info_dict() if self.config.referee_source == "gateway" else radar_info_to_dict(self.referee.latest_radar_info if self.referee else radar_info)),
            referee_state_age_s=post_rx_stage_decision.get("referee_state_age_s"),
            referee_state_fresh=bool(post_rx_stage_decision.get("referee_state_fresh")),
            **rx_result,
            recovered_key_vote_state=recovered_vote_state,
            key_upload_state=upload_result,
            stage_decision=status_stage_decision,
            last_error=rx_error or (self.last_error if self.config.referee_source == "gateway" else (self.referee.last_error if self.referee else self.last_error)),
        )
        self.loop_index += 1
        return status


def write_status(path: Path | None, status: WirelessLinkStatus | dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = status.to_dict() if isinstance(status, WirelessLinkStatus) else status
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
