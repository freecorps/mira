"""Verification through the real sandbox: a tiny Python repro, run isolated.

Skipped cleanly when this host has no working isolation (no bubblewrap on
Linux, or a container that denies it user namespaces). Everything else in the
verification tests mocks the sandbox; this is the one that proves the argv
Mira builds actually runs, actually fails a reproduction the right way, and
actually takes the network and Mira's files away.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from mira.models import ReviewComment, Severity
from mira.verify import reproduce
from mira.verify.config_models import VerifyTestRunner
from mira.verify.isolation import resolve_isolation
from mira.verify.policy import VerifyPolicy
from mira.verify.workspace import Workspace

_ISOLATION = resolve_isolation("auto")

pytestmark = pytest.mark.skipif(
    _ISOLATION is None, reason="no working verification sandbox on this host"
)

_SOURCE = {
    "calc/__init__.py": "",
    "calc/money.py": (
        "def split(total_cents: int, parts: int) -> list[int]:\n"
        "    share = total_cents // parts\n"
        "    return [share] * parts\n"
    ),
    "tests/__init__.py": "",
    "tests/test_money.py": (
        "from calc.money import split\n\n\ndef test_even():\n    assert split(10, 2) == [5, 5]\n"
    ),
}

_REPRO = (
    "from calc.money import split\n\n\n"
    "def test_remainder_is_lost():\n"
    "    shares = split(10, 3)\n"
    '    assert sum(shares) == 10, "MIRA-" + "REPRODUCED: " + repr(shares)\n'
)

_PASSING = (
    "from calc.money import split\n\n\n"
    "def test_even_split_is_exact():\n"
    '    assert sum(split(12, 3)) == 12, "MIRA-" + "REPRODUCED"\n'
)


def _runner() -> VerifyTestRunner:
    return VerifyTestRunner(
        name="pytest",
        extensions=[".py"],
        command=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "{test}"],
    )


@pytest.fixture
def workspace():
    assert _ISOLATION is not None
    ws = Workspace.create(_SOURCE, _ISOLATION, max_bytes=10_000_000)
    yield ws
    ws.cleanup()
    assert not ws.root.exists()


def test_reproduction_fails_on_its_marker(workspace: Workspace) -> None:
    import time

    workspace.write("tests/test_money_mira_repro_aa.py", _REPRO)
    answer, output, _ = reproduce.run_test(
        workspace,
        _runner(),
        "tests/test_money_mira_repro_aa.py",
        timeout_seconds=60,
        runs=2,
        deadline=time.monotonic() + 120,
    )
    assert answer == "reproduced", output
    assert "MIRA-REPRODUCED" in output


def test_passing_reproduction_is_not_reproduced(workspace: Workspace) -> None:
    import time

    workspace.write("tests/test_money_mira_repro_bb.py", _PASSING)
    answer, output, _ = reproduce.run_test(
        workspace,
        _runner(),
        "tests/test_money_mira_repro_bb.py",
        timeout_seconds=60,
        runs=1,
        deadline=time.monotonic() + 120,
    )
    assert answer == "passed", output


def test_the_canary_proves_the_harness_reports_failures(workspace: Workspace) -> None:
    import time

    trust = reproduce.check_canary(
        workspace,
        _runner(),
        like="tests/test_money_mira_repro_cc.py",
        timeout_seconds=60,
        deadline=time.monotonic() + 120,
    )
    assert trust.trusted, trust.reason
    # The canary is gone again: nothing Mira writes outlives its run.
    assert not any("mira_repro" in p.name for p in (workspace.repo / "tests").iterdir())


@pytest.mark.asyncio
async def test_a_whole_verification_through_the_sandbox(workspace: Workspace) -> None:
    import time

    class _LLM:
        async def complete_with_tools(self, messages, tools, temperature=None):
            import json

            return json.dumps({"feasible": True, "reason": "sums the shares", "test_code": _REPRO})

    comment = ReviewComment(
        path="calc/money.py",
        line=2,
        end_line=3,
        severity=Severity.WARNING,
        category="bug",
        title="split drops the remainder",
        body="10 split three ways returns [3, 3, 3]: a cent is lost.",
        confidence=0.8,
    )
    policy = VerifyPolicy(enabled=True, test_runners=(_runner(),), runs=2)
    from mira.autofix.finishing import detect_test_layout

    async def _trusted(_like: str) -> reproduce.HarnessTrust:
        return reproduce.HarnessTrust(True)

    attempt = reproduce.Attempt(comment=comment, runner=_runner())
    verification = await reproduce.verify_one(
        attempt,
        llm=_LLM(),
        workspace=workspace,
        layout=detect_test_layout(sorted(_SOURCE)),
        policy=policy,
        deadline=time.monotonic() + 120,
        harness=_trusted,
    )
    assert verification.outcome == "reproduced", verification
    assert verification.test_path.startswith("tests/test_money_mira_repro_")
    assert not workspace.exists(verification.test_path)


def test_no_network_after_setup(workspace: Workspace) -> None:
    """A test cannot open a socket: the network is gone once setup is over."""
    script = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except OSError as exc:\n"
        "    print('BLOCKED', exc)\n"
    )
    workspace.write("probe.py", script)
    outcome = workspace.run([sys.executable, "probe.py"], timeout_seconds=30, network=False)
    assert outcome.status == "ok", outcome.detail
    assert "BLOCKED" in outcome.stdout, outcome.stdout + outcome.stderr


def test_writes_outside_the_workspace_are_refused(workspace: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "escaped.txt"
    script = (
        "import sys\n"
        "try:\n"
        "    open(sys.argv[1], 'w').write('x')\n"
        "    print('WROTE')\n"
        "except OSError as exc:\n"
        "    print('REFUSED', exc)\n"
    )
    workspace.write("escape.py", script)
    outcome = workspace.run(
        [sys.executable, "escape.py", str(target)], timeout_seconds=30, network=False
    )
    assert outcome.status == "ok", outcome.detail
    assert not target.exists()


def test_the_environment_carries_no_secrets(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_never_be_seen")
    workspace.write("env.py", "import os\nprint(sorted(os.environ))\nprint(os.environ['HOME'])\n")
    outcome = workspace.run([sys.executable, "env.py"], timeout_seconds=30)
    assert outcome.status == "ok", outcome.detail
    assert "GITHUB_TOKEN" not in outcome.stdout
    assert str(workspace.home) in outcome.stdout or os.path.realpath(workspace.home) in (
        outcome.stdout
    )


def test_mira_home_directory_is_unreadable(workspace: Workspace) -> None:
    """The code under test cannot list Mira's own home directory."""
    assert _ISOLATION is not None
    if not _ISOLATION.confines_filesystem:
        pytest.skip("this isolation mode does not confine the filesystem")
    script = (
        "import os, sys\n"
        "try:\n"
        "    os.listdir(sys.argv[1])\n"
        "    print('LISTED')\n"
        "except OSError as exc:\n"
        "    print('REFUSED', exc)\n"
    )
    workspace.write("home.py", script)
    outcome = workspace.run(
        [sys.executable, "home.py", os.path.expanduser("~")], timeout_seconds=30
    )
    assert outcome.status == "ok", outcome.detail
    assert "REFUSED" in outcome.stdout, outcome.stdout + outcome.stderr
