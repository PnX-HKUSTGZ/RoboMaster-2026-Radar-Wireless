#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import sys


def _run_iio_attr(args: list[str], *, timeout: float) -> str | None:
    completed = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.stdout.strip() or None


def ensure_pluto_ensm_fdd(uri: str, *, timeout: float = 3.0) -> str:
    if not uri:
        return "skipped:no-uri"

    tool = shutil.which("iio_attr")
    if tool is None:
        return "skipped:no-iio_attr"

    base_cmd = [tool, "-u", uri, "-d", "ad9361-phy", "ensm_mode"]

    try:
        current_mode = _run_iio_attr(base_cmd, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"[pluto-ensm] failed to read ensm_mode for {uri}: {exc}\n")
        return f"read_failed:{type(exc).__name__}"

    if current_mode == "fdd":
        return "fdd"

    try:
        requested_mode = _run_iio_attr(base_cmd + ["fdd"], timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(
            f"[pluto-ensm] failed to switch {uri} ensm_mode from {current_mode or 'unknown'} to fdd: {exc}\n"
        )
        return f"set_failed:{type(exc).__name__}"

    if requested_mode == "fdd":
        return "fdd"

    try:
        verified_mode = _run_iio_attr(base_cmd, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        verified_mode = None
    return verified_mode or "unknown"
