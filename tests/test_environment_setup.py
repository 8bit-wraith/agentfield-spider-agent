"""Behavior tests for .environment/setup.sh docker privilege handling."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = ROOT_DIR / ".environment" / "setup.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_setup(tmp_path: Path, *, strict_mode: str) -> subprocess.CompletedProcess[str]:
    fake_python = tmp_path / "fake-python.sh"
    pip_log = tmp_path / "pip.log"
    pip_log.write_text("")

    _write_executable(
        fake_python,
        """#!/bin/bash
set -euo pipefail

if [[ "${1:-}" == "--version" ]]; then
    echo "Python 3.12.0"
    exit 0
fi

if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then
    shift 2
    echo "$*" >> "${SPIDER_TEST_PIP_LOG:?}"
    exit 0
fi

echo "unexpected fake-python arguments: $*" >&2
exit 1
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PYTHON_BIN": str(fake_python),
            "SPIDER_TEST_PIP_LOG": str(pip_log),
            "SPIDER_INSTALL_DOCKER": "1",
            "SPIDER_INSTALL_DOCKER_STRICT": strict_mode,
            "SPIDER_INSTALL_DOCLING": "0",
            "SPIDER_SETUP_PRIVILEGE_MODE": "none",
        }
    )

    return subprocess.run(
        ["/bin/bash", str(SETUP_SCRIPT)],
        cwd=ROOT_DIR,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_setup_warns_and_continues_when_docker_needs_privilege(tmp_path: Path) -> None:
    result = _run_setup(tmp_path, strict_mode="0")
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "WARN: docker install requested but no root access or passwordless sudo is available" in output
    assert "[env] setup complete" in output

    pip_log = (tmp_path / "pip.log").read_text()
    assert "install --upgrade pip setuptools wheel" in pip_log
    assert "install -r requirements.txt" in pip_log
    assert "check" in pip_log


def test_setup_fails_in_strict_mode_without_elevation(tmp_path: Path) -> None:
    result = _run_setup(tmp_path, strict_mode="1")
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "ERROR: docker install requested but no root access or passwordless sudo is available" in output
    assert (tmp_path / "pip.log").read_text() == ""
