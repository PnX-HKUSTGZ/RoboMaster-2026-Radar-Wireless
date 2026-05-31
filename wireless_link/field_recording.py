from __future__ import annotations

from collections import Counter
from pathlib import Path

from gnuradio import blocks, digital, filter, gr

from .decoder import (
    extract_info_economics,
    extract_info_positions,
    extract_info_remaining_bullets,
    recover_jam_key_from_packets,
)
from .protocol import (
    AIR_BIT_ORDER_AUTO,
    AIR_BIT_ORDER_CHOICES,
    INFO_ACCESS_CODE,
    INFO_SAMPLE_RATE,
    INFO_SENSITIVITY,
    INFO_SPS,
    JAM_ACCESS_CODE,
    JAM_PROFILES,
    TEAM_BLUE,
    TEAM_RED,
    iter_referee_frames,
    recover_air_packets_from_bits,
    reassemble_stream_from_packets,
    summarize_frames,
)

PLUTO_SAMPLE_RATE = 2_083_333.0
PLUTO_RESAMPLE_INTERP = 12
PLUTO_RESAMPLE_DECIM = 25


class FieldRecordingDemod(gr.top_block):
    def __init__(
        self,
        *,
        path: Path,
        mode: str,
        team: str,
        jam_level: int,
        sample_rate: float,
        max_samples: int,
        rx_preprocess: str,
        gain_mu: float,
        omega_relative_limit: float,
        symbol_freq_error: float,
    ):
        super().__init__("rm2026_wireless_field_recording_demod")
        self.src = blocks.file_source(gr.sizeof_gr_complex, str(path), False)
        head = self.src
        if max_samples > 0:
            self.head = blocks.head(gr.sizeof_gr_complex, int(max_samples))
            self.connect(head, self.head)
            head = self.head
        if rx_preprocess == "dc_block":
            self.dc_block = filter.dc_blocker_cc(32, True)
            self.connect(head, self.dc_block)
            head = self.dc_block
        elif rx_preprocess != "off":
            raise ValueError(f"unsupported rx_preprocess: {rx_preprocess}")

        if mode == "info":
            sensitivity = INFO_SENSITIVITY
            # Keep offline info replay aligned with the live Pluto chain.
            # The narrower passband avoids pulling nearby L3 jam energy into
            # clock recovery while still preserving the official info channel.
            cutoff_hz = 260_000.0
            transition_hz = 60_000.0
        else:
            profile = JAM_PROFILES[team][jam_level]
            sensitivity = float(profile["sensitivity"])
            if jam_level == 3:
                cutoff_hz = 125_000.0
                transition_hz = 20_000.0
            else:
                cutoff_hz = 500_000.0
                transition_hz = 70_000.0

        self.channel_filter = filter.fir_filter_ccf(
            1,
            list(filter.firdes.low_pass(1.0, float(sample_rate), cutoff_hz, transition_hz)),
        )
        if abs(float(sample_rate) - PLUTO_SAMPLE_RATE) < 1.0:
            interpolation = PLUTO_RESAMPLE_INTERP
            decimation = PLUTO_RESAMPLE_DECIM
        else:
            interpolation = 1
            decimation = max(1, round(float(sample_rate) / INFO_SAMPLE_RATE))

        self.resamp = filter.rational_resampler_ccc(
            interpolation=int(interpolation),
            decimation=int(decimation),
            taps=[],
            fractional_bw=0.0,
        )
        self.demod = digital.gfsk_demod(
            samples_per_symbol=INFO_SPS,
            sensitivity=sensitivity,
            gain_mu=gain_mu,
            mu=0.5,
            omega_relative_limit=omega_relative_limit,
            freq_error=symbol_freq_error,
            verbose=False,
            log=False,
        )
        self.sink = blocks.vector_sink_b()
        self.connect(head, self.channel_filter, self.resamp, self.demod, self.sink)

    def bits(self) -> list[int]:
        return [int(item) & 0x01 for item in self.sink.data()]


def _decode_once(bits: list[int], *, mode: str, air_bit_order: str, polarity: str) -> dict:
    selected_bits = [bit ^ 1 for bit in bits] if polarity == "inverted" else bits
    access_code = INFO_ACCESS_CODE if mode == "info" else JAM_ACCESS_CODE
    packets = recover_air_packets_from_bits(selected_bits, access_code=access_code, air_bit_order=air_bit_order)
    frames = list(iter_referee_frames(reassemble_stream_from_packets(packets)))
    summaries = summarize_frames(frames)
    counts = Counter(frame["cmd_id"] for frame in summaries)
    crc8_counts = Counter(frame.get("crc8_mode", "unknown") for frame in summaries)
    recovered_jam_key = recover_jam_key_from_packets(packets) if mode == "jam" else None
    partial_frames = []
    if mode == "info":
        from .decoder import scan_partial_info_frames

        partial_frames = scan_partial_info_frames(packets)
    partial_counts = Counter(frame["cmd_id"] for frame in partial_frames)
    plausible_partial_counts = Counter(frame["cmd_id"] for frame in partial_frames if frame.get("plausible"))
    info_positions_cm, info_positions_source = (
        extract_info_positions(summaries, partial_frames) if mode == "info" else ({}, {})
    )
    info_economics = extract_info_economics(summaries, partial_frames) if mode == "info" else {}
    info_remaining_bullets = extract_info_remaining_bullets(summaries, partial_frames) if mode == "info" else {}
    return {
        "selected_air_bit_order": air_bit_order,
        "bit_polarity": polarity,
        "air_packets_found": len(packets),
        "referee_frames_found": len(summaries),
        "frame_counts": dict(sorted(counts.items())),
        "crc8_mode_counts": dict(sorted(crc8_counts.items())),
        "recovered_jam_key": recovered_jam_key,
        "partial_referee_frames_found": len(partial_frames),
        "partial_frame_counts": dict(sorted(partial_counts.items())),
        "plausible_partial_frame_counts": dict(sorted(plausible_partial_counts.items())),
        "partial_referee_frames": partial_frames,
        "info_positions_cm": info_positions_cm,
        "info_positions_source": info_positions_source,
        "info_economics": info_economics,
        "info_remaining_bullets": info_remaining_bullets,
        "frames": summaries,
    }


def _score(result: dict) -> tuple[int, int, int]:
    return (
        int(result.get("referee_frames_found", 0)),
        len(result.get("frame_counts", {})),
        sum(int(value) for value in result.get("plausible_partial_frame_counts", {}).values()),
        int(result.get("partial_referee_frames_found", 0)),
        int(result.get("air_packets_found", 0)),
    )


def decode_bits(bits: list[int], *, mode: str, air_bit_order: str) -> dict:
    orders = AIR_BIT_ORDER_CHOICES if air_bit_order == AIR_BIT_ORDER_AUTO else (air_bit_order,)
    candidates = []
    for order in orders:
        for polarity in ("normal", "inverted"):
            candidates.append(_decode_once(bits, mode=mode, air_bit_order=order, polarity=polarity))
    candidates.sort(key=_score, reverse=True)
    best = candidates[0] if candidates else {}
    return {
        "requested_air_bit_order": air_bit_order,
        "recommended_air_bit_order": best.get("selected_air_bit_order"),
        "recommended_bit_polarity": best.get("bit_polarity"),
        "best": best,
        "candidates": [
            {
                "selected_air_bit_order": item["selected_air_bit_order"],
                "bit_polarity": item["bit_polarity"],
                "air_packets_found": item["air_packets_found"],
                "referee_frames_found": item["referee_frames_found"],
                "frame_counts": item["frame_counts"],
                "crc8_mode_counts": item["crc8_mode_counts"],
                "recovered_jam_key": item.get("recovered_jam_key"),
                "partial_referee_frames_found": item.get("partial_referee_frames_found", 0),
                "partial_frame_counts": item.get("partial_frame_counts", {}),
                "plausible_partial_frame_counts": item.get("plausible_partial_frame_counts", {}),
            }
            for item in candidates
        ],
    }


def parse_field_recording(
    *,
    iq_in: Path,
    mode: str = "jam",
    team: str = TEAM_BLUE,
    jam_level: int = 3,
    sample_rate: float = 2_000_000.0,
    air_bit_order: str = AIR_BIT_ORDER_AUTO,
    rx_preprocess: str = "off",
    max_samples: int = 0,
    gain_mu: float = 0.175,
    omega_relative_limit: float = 0.005,
    symbol_freq_error: float = 0.0048,
) -> dict:
    byte_count = iq_in.stat().st_size
    demod = FieldRecordingDemod(
        path=iq_in,
        mode=mode,
        team=team,
        jam_level=jam_level,
        sample_rate=sample_rate,
        max_samples=max_samples,
        rx_preprocess=rx_preprocess,
        gain_mu=gain_mu,
        omega_relative_limit=omega_relative_limit,
        symbol_freq_error=symbol_freq_error,
    )
    demod.run()
    bits = demod.bits()
    decoded = decode_bits(bits, mode=mode, air_bit_order=air_bit_order)
    return {
        "iq_in": str(iq_in),
        "mode": mode,
        "team": team,
        "jam_level": int(jam_level),
        "sample_rate": float(sample_rate),
        "seconds": float(byte_count / 8.0 / sample_rate),
        "rx_preprocess": rx_preprocess,
        "demod_bits": len(bits),
        "decode": decoded,
    }


def field_regression(
    *,
    l3_iq_in: Path = Path("recordings/data/20260530_182117/wireless/info_20260530_182247_loop000023.c64"),
    l2_iq_in: Path = Path("recordings/data/20260530_182117/wireless/jam2_20260530_182343_loop000002.c64"),
    expected_key: str = "fcYqTC",
    min_frames: int = 1,
) -> dict:
    l3_output = parse_field_recording(iq_in=l3_iq_in, mode="jam", team=TEAM_BLUE, jam_level=3)
    decoded = l3_output["decode"]
    best = decoded["best"]
    keys = [
        frame.get("decoded", {}).get("jam_key")
        for frame in best.get("frames", [])
        if frame.get("cmd_id") == "0x0A06"
    ]

    l2_output = parse_field_recording(iq_in=l2_iq_in, mode="jam", team=TEAM_BLUE, jam_level=2)
    l2_decoded = l2_output["decode"]
    l2_best = l2_decoded["best"]
    l2_recovered = l2_best.get("recovered_jam_key") or {}
    return {
        "iq_in": str(l3_iq_in),
        "l2_iq_in": str(l2_iq_in),
        "expected_key": expected_key,
        "recommended_air_bit_order": decoded.get("recommended_air_bit_order"),
        "recommended_bit_polarity": decoded.get("recommended_bit_polarity"),
        "air_packets_found": best.get("air_packets_found", 0),
        "referee_frames_found": best.get("referee_frames_found", 0),
        "frame_counts": best.get("frame_counts", {}),
        "crc8_mode_counts": best.get("crc8_mode_counts", {}),
        "keys_preview": keys[:12],
        "l2_recommended_air_bit_order": l2_decoded.get("recommended_air_bit_order"),
        "l2_air_packets_found": l2_best.get("air_packets_found", 0),
        "l2_referee_frames_found": l2_best.get("referee_frames_found", 0),
        "l2_recovered_jam_key": l2_recovered,
        "passed": (
            decoded.get("recommended_air_bit_order") == "msb_all"
            and decoded.get("recommended_bit_polarity") == "normal"
            and int(best.get("referee_frames_found", 0)) >= min_frames
            and best.get("crc8_mode_counts", {}).get("reflected", 0) >= min_frames
            and expected_key in keys
            and l2_decoded.get("recommended_air_bit_order") == "msb_all"
            and int(l2_best.get("referee_frames_found", 0)) == 0
            and l2_recovered.get("recovered_key") == expected_key
        ),
    }
