#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any


INFO_FRAME_NAMES = {
    "0x0A01": "position",
    "0x0A02": "hp",
    "0x0A03": "bullet",
    "0x0A04": "macro",
    "0x0A05": "buff",
    "0x0A06": "jam_key",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RM2026 wireless-link status dashboard.")
    parser.add_argument("--wireless-status", type=Path, required=True)
    parser.add_argument("--gateway-status", type=Path)
    parser.add_argument("--refresh-ms", type=int, default=500)
    parser.add_argument("--title", default="RM2026 Wireless Status")
    return parser.parse_args()


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"read_error": str(exc)}


def age_text(timestamp_s: Any) -> str:
    try:
        age = time.time() - float(timestamp_s)
    except (TypeError, ValueError):
        return "unknown"
    return f"{age:.1f}s ago"


def fmt_bool(value: Any) -> str:
    if value is None:
        return "unknown"
    return "yes" if bool(value) else "no"


def fmt_freq(value: Any) -> str:
    try:
        return f"{float(value) / 1_000_000.0:.3f} MHz"
    except (TypeError, ValueError):
        return "unknown"


def fmt_number(value: Any, digits: int = 3) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "unknown"


def cmd_counts_text(counts: dict[str, Any]) -> str:
    if not counts:
        return "none"
    parts = []
    for key in sorted(counts):
        label = INFO_FRAME_NAMES.get(str(key), str(key))
        parts.append(f"{key}({label})={counts[key]}")
    return ", ".join(parts)


def min_hamming_text(status: dict[str, Any]) -> str:
    diagnostics = status.get("access_code_diagnostics") or {}
    values = []
    for polarity in ("normal", "inverted"):
        item = diagnostics.get(polarity) or {}
        if "min_hamming" in item:
            values.append(f"{polarity}:{item['min_hamming']}")
    return ", ".join(values) if values else "unknown"


class WirelessStatusDashboard:
    def __init__(self, root: tk.Tk, args: argparse.Namespace) -> None:
        self.root = root
        self.args = args
        self.last_by_mode: dict[str, dict[str, Any]] = {}

        root.title(args.title)
        root.geometry("980x720")
        root.configure(bg="#111820")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#111820")
        style.configure("Title.TLabel", background="#111820", foreground="#e8f3ff", font=("DejaVu Sans", 18, "bold"))
        style.configure("Meta.TLabel", background="#111820", foreground="#9fb3c8", font=("DejaVu Sans", 10))

        outer = ttk.Frame(root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X)
        self.title_label = ttk.Label(header, text=args.title, style="Title.TLabel")
        self.title_label.pack(side=tk.LEFT)
        self.meta_label = ttk.Label(header, text="", style="Meta.TLabel")
        self.meta_label.pack(side=tk.RIGHT)

        self.text = tk.Text(
            outer,
            wrap=tk.WORD,
            bg="#071018",
            fg="#d9e7f2",
            insertbackground="#d9e7f2",
            selectbackground="#20435c",
            font=("DejaVu Sans Mono", 11),
            relief=tk.FLAT,
            padx=14,
            pady=12,
        )
        self.text.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        self.text.tag_configure("ok", foreground="#62d394")
        self.text.tag_configure("warn", foreground="#ffd166")
        self.text.tag_configure("bad", foreground="#ff6b6b")
        self.text.tag_configure("section", foreground="#7cc7ff", font=("DejaVu Sans Mono", 12, "bold"))
        self.text.tag_configure("muted", foreground="#8da1b3")

    def run(self) -> None:
        self.refresh()
        self.root.mainloop()

    def refresh(self) -> None:
        wireless = read_json(self.args.wireless_status)
        gateway = read_json(self.args.gateway_status)
        mode = str(wireless.get("rx_mode") or "")
        if mode in ("jam", "info"):
            self.last_by_mode[mode] = dict(wireless)

        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.write_current(wireless, gateway)
        self.write_jam(self.last_by_mode.get("jam", {}))
        self.write_info(self.last_by_mode.get("info", {}))
        self.write_gateway(gateway)
        self.text.configure(state=tk.DISABLED)
        self.meta_label.configure(text=f"refresh {self.args.refresh_ms}ms | {time.strftime('%H:%M:%S')}")
        self.root.after(max(100, int(self.args.refresh_ms)), self.refresh)

    def line(self, text: str = "", tag: str | None = None) -> None:
        self.text.insert(tk.END, text + "\n", tag)

    def kv(self, key: str, value: Any, tag: str | None = None) -> None:
        self.text.insert(tk.END, f"{key:<30} {value}\n", tag)

    def write_current(self, status: dict[str, Any], gateway: dict[str, Any]) -> None:
        self.line("CURRENT RX", "section")
        if not status:
            self.line("wireless status file not ready", "warn")
            self.line()
            return
        if status.get("read_error"):
            self.line(f"wireless status read error: {status['read_error']}", "bad")
            self.line()
            return
        mode = status.get("rx_mode") or "unknown"
        frames = int(status.get("referee_frames_found") or 0)
        packets = int(status.get("air_packets_found") or 0)
        tag = "ok" if frames > 0 else ("warn" if packets > 0 else "bad")
        self.kv("updated", age_text(status.get("timestamp_s")))
        self.kv("team", status.get("team"))
        self.kv("stage / mode", f"{status.get('rx_stage')} / {mode}", tag)
        self.kv("center freq", fmt_freq(status.get("rx_center_freq_hz")))
        self.kv("rf bandwidth", fmt_freq(status.get("rf_bandwidth_hz")))
        self.kv("air packets / frames", f"{packets} / {frames}", tag)
        self.kv("bit order / polarity", f"{status.get('selected_air_bit_order')} / {status.get('bit_polarity')}")
        self.kv("access min hamming", min_hamming_text(status))
        self.kv("referee radar info", gateway.get("radar_info") if gateway else None)
        self.kv("last error", status.get("last_error") or status.get("rx_error") or "none")
        self.line()

    def write_jam(self, status: dict[str, Any]) -> None:
        self.line("JAM WAVE", "section")
        if not status:
            self.line("no jam-wave sample yet", "muted")
            self.line()
            return
        detail = status.get("recovered_key_detail") or {}
        vote_state = status.get("recovered_key_vote_state") or {}
        confidence = detail.get("confidence")
        key = status.get("strict_key") or status.get("recovered_key")
        tag = "ok" if key else "warn"
        self.kv("updated", age_text(status.get("timestamp_s")))
        self.kv("stage / level", f"{status.get('rx_stage')} / {status.get('jam_level')}")
        self.kv("strict key", status.get("strict_key") or "none", tag if status.get("strict_key") else None)
        self.kv("recovered key", status.get("recovered_key") or "none", tag)
        self.kv(
            "recovered vote",
            f"{vote_state.get('buffered_windows', 0)}/{vote_state.get('target_windows', '?')} windows, "
            f"candidates={vote_state.get('candidate_count', 0)}, reason={vote_state.get('reason', 'unknown')}",
        )
        self.kv("confidence", fmt_number(confidence, 2) if confidence is not None else "unknown")
        self.kv("key upload", status.get("key_upload_state"))
        self.kv("packets / frames", f"{status.get('air_packets_found')} / {status.get('referee_frames_found')}")
        self.kv("access min hamming", min_hamming_text(status))
        self.line()

    def write_info(self, status: dict[str, Any]) -> None:
        self.line("INFO WAVE", "section")
        if not status:
            self.line("no info-wave sample yet", "muted")
            self.line()
            return
        counts = status.get("info_frame_counts") or status.get("frame_counts") or {}
        frames = int(status.get("referee_frames_found") or 0)
        tag = "ok" if frames > 0 else "warn"
        self.kv("updated", age_text(status.get("timestamp_s")))
        self.kv("stage", status.get("rx_stage"))
        self.kv("packets / frames", f"{status.get('air_packets_found')} / {frames}", tag)
        self.kv("frame counts", cmd_counts_text(counts), tag if counts else None)
        self.kv("center freq", fmt_freq(status.get("rx_center_freq_hz")))
        self.kv("access min hamming", min_hamming_text(status))
        self.line()

    def write_gateway(self, gateway: dict[str, Any]) -> None:
        self.line("REFEREE GATEWAY", "section")
        if not gateway:
            self.line("gateway status file not ready", "warn")
            return
        if gateway.get("read_error"):
            self.line(f"gateway status read error: {gateway['read_error']}", "bad")
            return
        self.kv("updated", age_text(gateway.get("timestamp_s")))
        self.kv("online", fmt_bool(gateway.get("online")), "ok" if gateway.get("online") else "bad")
        self.kv("rx / tx count", f"{gateway.get('rx_count')} / {gateway.get('tx_count')}")
        self.kv("radar info", gateway.get("radar_info"))
        self.kv("last tx", gateway.get("last_tx_result"))
        self.kv("last error", gateway.get("last_error") or "none")


def main() -> int:
    args = parse_args()
    root = tk.Tk()
    WirelessStatusDashboard(root, args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
