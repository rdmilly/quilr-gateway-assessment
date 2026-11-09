"""The reviewer tour (python -m demo) runs end to end and every check in it passes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_demo_tour_passes_every_check() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "demo"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All checks passed" in result.stdout
    assert "[FAIL]" not in result.stdout
