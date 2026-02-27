# bot/utils/probe.py
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from ..config import FFPROBE_BIN

def media_duration_seconds(path: Path) -> Optional[float]:
    """
    Returns duration in seconds if ffprobe can read it.
    """
    try:
        cmd = [
            FFPROBE_BIN, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=25)
        data = json.loads(out.decode("utf-8", errors="ignore"))
        dur = data.get("format", {}).get("duration")
        if dur is None:
            return None
        return float(dur)
    except Exception:
        return None
