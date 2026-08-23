"""Contract tests for the Orange Pi image updater."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="the deployed updater is a Linux/bash systemd service",
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_failed_candidate_rolls_back_previous_image(tmp_path: Path) -> None:
    """A failed candidate must recreate the service with the prior image."""
    repo_root = Path(__file__).resolve().parents[1]
    updater = repo_root / "deploy" / "orangepi" / "mira-update.sh"
    stack_dir = tmp_path / "stack"
    stack_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state_file = tmp_path / "docker-state"
    state_file.touch()

    _write_executable(
        bin_dir / "docker",
        """#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$1 $2" == "image inspect" ]]; then
  if grep -q '^pulled$' "$MIRA_TEST_STATE"; then
    echo 'sha256:candidate'
  else
    echo 'sha256:previous'
  fi
elif [[ "$1 $2 $3" == "compose ps -q" ]]; then
  echo 'running-container'
elif [[ "$1" == "inspect" ]]; then
  echo 'sha256:previous'
elif [[ "$1 $2 $3" == "compose pull mira" ]]; then
  echo 'pulled' >> "$MIRA_TEST_STATE"
elif [[ "$1 $2" == "compose up" && " $* " == *" --force-recreate "* ]]; then
  echo 'rollback-started' >> "$MIRA_TEST_STATE"
elif [[ "$1 $2" == "compose up" ]]; then
  echo 'candidate-started' >> "$MIRA_TEST_STATE"
elif [[ "$1 $2" == "image tag" ]]; then
  echo 'previous-tagged' >> "$MIRA_TEST_STATE"
elif [[ "$1 $2" == "compose logs" ]]; then
  exit 0
else
  echo "unexpected docker invocation: $*" >&2
  exit 64
fi
""",
    )
    _write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env bash
set -Eeuo pipefail
grep -q '^rollback-started$' "$MIRA_TEST_STATE"
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "MIRA_STACK_DIR": str(stack_dir),
            "MIRA_SERVICE": "mira",
            "MIRA_IMAGE": "ghcr.io/test/mira:edge",
            "MIRA_UPDATE_LOCK_FILE": str(tmp_path / "updater.lock"),
            "MIRA_HEALTH_ATTEMPTS": "1",
            "MIRA_HEALTH_INTERVAL_SECONDS": "0",
            "MIRA_TEST_STATE": str(state_file),
        }
    )

    result = subprocess.run(
        ["bash", str(updater)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    # A successful rollback still exits non-zero so systemd reports that the
    # attempted update failed instead of silently treating it as a deployment.
    assert result.returncode == 1
    assert "Rollback is healthy" in result.stdout
    assert state_file.read_text().splitlines() == [
        "pulled",
        "candidate-started",
        "previous-tagged",
        "rollback-started",
    ]
