from __future__ import annotations

import math
from pathlib import Path

from gnuradio import analog, blocks, digital, filter, gr, iio

from .pluto_ensm import ensure_pluto_ensm_fdd
from .protocol import INFO_SAMPLE_RATE, INFO_SPS

PLUTO_SAMPLE_RATE = 2_083_333
DOWNSAMPLE_INTERP = 12
DOWNSAMPLE_DECIM = 25


class PlutoWirelessRx(gr.top_block):
    def __init__(
        self,
        uri: str,
        center_freq: int,
        rf_bandwidth: int,
        gain_mode: str,
        manual_gain: float,
        sensitivity: float,
        gain_mu: float = 0.175,
        omega_relative_limit: float = 0.005,
        freq_error: float = 0.0048,
        dc_blocker_length: int = 0,
        probe_decim: int = 100,
        record_iq_path: str | None = None,
    ):
        super().__init__("rm2026_wireless_pluto_rx")
        self.sensitivity = sensitivity
        self.probe_decim = max(1, probe_decim)
        self.ensm_status = ensure_pluto_ensm_fdd(uri)
        self.source = iio.fmcomms2_source_fc32(uri, [True, True], 32768)
        self.source.set_len_tag_key("packet_len")
        self.source.set_frequency(center_freq)
        self.source.set_samplerate(PLUTO_SAMPLE_RATE)
        self.source.set_gain_mode(0, gain_mode)
        self.source.set_gain(0, manual_gain)
        self.source.set_quadrature(True)
        self.source.set_rfdc(True)
        self.source.set_bbdc(True)
        self.source.set_filter_params("Auto", "", 0, 0)
        self.iq_file_sink = None
        if record_iq_path:
            record_path = Path(record_iq_path)
            record_path.parent.mkdir(parents=True, exist_ok=True)
            self.iq_file_sink = blocks.file_sink(gr.sizeof_gr_complex, str(record_path), False)

        self.resamp = filter.rational_resampler_ccc(
            interpolation=DOWNSAMPLE_INTERP,
            decimation=DOWNSAMPLE_DECIM,
            taps=[],
            fractional_bw=0.0,
        )
        self.fmdemod = analog.quadrature_demod_cf(1.0 / sensitivity)
        omega = INFO_SPS * (1.0 + freq_error)
        gain_omega = 0.25 * gain_mu * gain_mu
        loop_bw = -math.log((gain_mu + gain_omega) / (-2.0) + 1.0)
        max_dev = omega_relative_limit * INFO_SPS
        self.dc_blocker = filter.dc_blocker_ff(dc_blocker_length, True) if dc_blocker_length > 0 else None
        self.clock_recovery = digital.symbol_sync_ff(
            digital.TED_MUELLER_AND_MULLER,
            omega,
            loop_bw,
            1.0,
            1.0,
            max_dev,
            1,
            digital.constellation_bpsk().base(),
            digital.IR_MMSE_8TAP,
            128,
            [],
        )
        self.slicer = digital.binary_slicer_fb()
        self.sink = blocks.vector_sink_b()

        self.iq_probe_decim = blocks.keep_one_in_n(gr.sizeof_gr_complex, self.probe_decim)
        self.iq_probe = blocks.vector_sink_c()
        self.raw_fm_probe_decim = blocks.keep_one_in_n(gr.sizeof_float, self.probe_decim)
        self.raw_fm_probe = blocks.vector_sink_f()
        self.demod_probe_decim = blocks.keep_one_in_n(gr.sizeof_float, self.probe_decim)
        self.demod_probe = blocks.vector_sink_f()

        self.connect((self.source, 0), self.resamp, self.fmdemod)
        if self.iq_file_sink is not None:
            self.connect((self.source, 0), self.iq_file_sink)
        self.connect(self.resamp, self.iq_probe_decim, self.iq_probe)
        self.connect(self.fmdemod, self.raw_fm_probe_decim, self.raw_fm_probe)
        if self.dc_blocker is not None:
            self.connect(self.fmdemod, self.dc_blocker, self.clock_recovery, self.slicer, self.sink)
            self.connect(self.dc_blocker, self.demod_probe_decim, self.demod_probe)
        else:
            self.connect(self.fmdemod, self.clock_recovery, self.slicer, self.sink)
            self.connect(self.fmdemod, self.demod_probe_decim, self.demod_probe)

    def bits(self) -> list[int]:
        return [int(x) & 0x01 for x in self.sink.data()]

    def release_resources(self) -> None:
        if self.iq_file_sink is not None:
            close = getattr(self.iq_file_sink, "close", None)
            if callable(close):
                close()
        disconnect_all = getattr(self, "disconnect_all", None)
        if callable(disconnect_all):
            disconnect_all()

    def iq_stats(self) -> dict:
        return _complex_stats(self.iq_probe.data())

    def raw_fm_stats(self) -> dict:
        stats = _float_stats(self.raw_fm_probe.data())
        if stats["count"]:
            stats["estimated_freq_offset_hz"] = stats["mean"] * self.sensitivity * INFO_SAMPLE_RATE / (2.0 * math.pi)
        return stats

    def demod_stats(self) -> dict:
        return _float_stats(self.demod_probe.data())


def _complex_stats(samples: tuple[complex, ...]) -> dict:
    count = len(samples)
    if count == 0:
        return {"count": 0}

    abs_sum = 0.0
    power_sum = 0.0
    max_abs = 0.0
    real_sum = 0.0
    imag_sum = 0.0
    for sample in samples:
        real_sum += sample.real
        imag_sum += sample.imag
        magnitude = abs(sample)
        abs_sum += magnitude
        power_sum += magnitude * magnitude
        max_abs = max(max_abs, magnitude)

    return {
        "count": count,
        "mean_abs": abs_sum / count,
        "rms": math.sqrt(power_sum / count),
        "max_abs": max_abs,
        "mean_i": real_sum / count,
        "mean_q": imag_sum / count,
    }


def _float_stats(samples: tuple[float, ...]) -> dict:
    count = len(samples)
    if count == 0:
        return {"count": 0}

    total = 0.0
    power_sum = 0.0
    min_value = float("inf")
    max_value = float("-inf")
    for sample in samples:
        value = float(sample)
        total += value
        power_sum += value * value
        min_value = min(min_value, value)
        max_value = max(max_value, value)

    mean = total / count
    return {
        "count": count,
        "mean": mean,
        "rms": math.sqrt(power_sum / count),
        "min": min_value,
        "max": max_value,
    }
