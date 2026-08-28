"""Phase 7C — the CODEOWNERS signal, and the ref it is read at.

The security property of this phase lives here. CODEOWNERS is repository
policy; the pull request is the thing being measured against it. A branch that
could add ``src/ @friendly-account`` and be ranked under it would be choosing
its own reviewer, so ownership is read at the **base** and a pull request whose
base is unknown gets no ownership signal rather than a head-read one.

The other half is the distinction the whole phase turns on: an unreadable
CODEOWNERS is ``unavailable`` and an absent one is ``empty``. Collapsing them
would let a broken ownership map read as "nobody owns this".
"""

from __future__ import annotations

from typing import Any

import pytest

from mira.gate.codeowners import parse as parse_codeowners
from mira.models import PRInfo
from mira.triage import ownership
from mira.triage.models import Evidence

OWNERS_TEXT = (
    "# The platform team owns the runtime\n"
    "src/mira/ @acme/platform\n"
    "src/mira/checks/ @dana\n"
    "docs/ @sam docs@acme.example\n"
    "src/mira/checks/vendor/\n"
)


def _pr(**overrides: Any) -> PRInfo:
    fields: dict[str, Any] = {
        "title": "Tighten the ingest limiter",
        "description": "",
        "base_branch": "main",
        "head_branch": "feature/limit",
        "url": "https://github.com/acme/app/pull/7",
        "number": 7,
        "owner": "acme",
        "repo": "app",
        "base_sha": "base111",
        "head_sha": "head222",
    }
    fields.update(overrides)
    return PRInfo(**fields)


class FakeProvider:
    """Records the ref every CODEOWNERS read asked for."""

    def __init__(self, text: str | None = OWNERS_TEXT, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.refs: list[str] = []

    async def get_codeowners(self, pr_info: PRInfo, ref: str = "") -> tuple[str, str]:
        self.refs.append(ref)
        if self.error is not None:
            raise self.error
        if self.text is None:
            return "", ""
        return ".github/CODEOWNERS", self.text


class RefBlindProvider:
    """A provider whose CODEOWNERS reader cannot be pointed at a ref."""

    async def get_codeowners(self, pr_info: PRInfo) -> tuple[str, str]:  # pragma: no cover
        raise AssertionError("must not be called")


async def test_ownership_is_read_at_the_base_commit() -> None:
    provider = FakeProvider()
    outcome = await ownership.gather(provider, _pr(), ["src/mira/checks/runner.py"])
    assert provider.refs == ["base111"]
    assert outcome.ref == "base111"
    assert outcome.report.status == "available"
    assert "dana" in {owner.lstrip("@") for owner in outcome.owners}


async def test_a_pull_request_cannot_nominate_its_own_reviewer() -> None:
    """The head ref is never consulted, even as a fallback.

    A provider that returned the *head* CODEOWNERS would let a contributor add
    a line naming an account they control and be ranked under it. The ref asked
    for is the base's, and the run records which one that was.
    """
    provider = FakeProvider()
    await ownership.gather(provider, _pr(), ["src/mira/checks/runner.py"])
    assert "head222" not in provider.refs


async def test_an_unknown_base_produces_no_ownership_rather_than_a_head_read() -> None:
    provider = FakeProvider()
    outcome = await ownership.gather(
        provider, _pr(base_sha="", base_branch=""), ["src/mira/checks/runner.py"]
    )
    assert provider.refs == []
    assert outcome.report.status == "unavailable"
    assert "base commit is unknown" in outcome.report.detail


async def test_a_provider_that_cannot_choose_a_ref_is_unsupported_not_head_read() -> None:
    outcome = await ownership.gather(RefBlindProvider(), _pr(), ["src/a.py"])
    assert outcome.report.status == "unsupported"
    assert "never read from the pull request's own head" in outcome.report.detail


async def test_no_codeowners_file_is_an_answer() -> None:
    outcome = await ownership.gather(FakeProvider(text=None), _pr(), ["src/a.py"])
    assert outcome.report.status == "empty"
    assert outcome.owners == {}


async def test_an_unreadable_codeowners_is_not_an_absent_one() -> None:
    outcome = await ownership.gather(
        FakeProvider(error=RuntimeError("502 from the API")), _pr(), ["src/a.py"]
    )
    assert outcome.report.status == "unavailable"
    assert "502" in outcome.report.detail


async def test_a_codeowners_the_parser_cannot_read_is_unavailable() -> None:
    """The strict parser the merge gate shares. A half-understood ownership map
    is worse than none, and here it must not read as 'nobody owns this'."""
    outcome = await ownership.gather(
        FakeProvider(text="src/ this-is-not-an-owner\n"), _pr(), ["src/a.py"]
    )
    assert outcome.report.status == "unavailable"
    assert "could not be parsed" in outcome.report.detail


async def test_a_repository_with_owners_for_nothing_changed_is_empty() -> None:
    outcome = await ownership.gather(FakeProvider(), _pr(), ["unrelated/file.txt"])
    assert outcome.report.status == "empty"
    assert outcome.owners == {}


async def test_the_signal_can_be_switched_off_without_looking_like_a_failure() -> None:
    provider = FakeProvider()
    outcome = await ownership.gather(provider, _pr(), ["src/a.py"], enabled=False)
    assert outcome.report.status == "disabled"
    assert provider.refs == []


async def test_a_platform_that_cannot_read_files_says_unsupported() -> None:
    outcome = await ownership.gather(FakeProvider(), _pr(), ["src/a.py"], can_read=False)
    assert outcome.report.status == "unsupported"


def test_every_owner_cites_the_line_that_made_them_one() -> None:
    parsed = parse_codeowners(OWNERS_TEXT, source_path=".github/CODEOWNERS")
    owners = ownership.owners_from(parsed, ["src/mira/checks/runner.py"])
    evidence: list[Evidence] = owners["@dana"]
    assert evidence[0].path == "src/mira/checks/runner.py"
    assert evidence[0].line == 3
    assert ".github/CODEOWNERS:3" in evidence[0].detail


def test_the_last_matching_rule_wins_as_git_does() -> None:
    parsed = parse_codeowners(OWNERS_TEXT, source_path="CODEOWNERS")
    owners = ownership.owners_from(parsed, ["src/mira/engine.py"])
    assert set(owners) == {"@acme/platform"}


def test_a_rule_with_no_owners_genuinely_un_owns_a_path() -> None:
    """Writing a bare pattern is how a repository carves out an exception."""
    parsed = parse_codeowners(OWNERS_TEXT, source_path="CODEOWNERS")
    assert ownership.owners_from(parsed, ["src/mira/checks/vendor/lib.py"]) == {}


def test_an_email_owner_is_kept_as_an_identity() -> None:
    parsed = parse_codeowners(OWNERS_TEXT, source_path="CODEOWNERS")
    owners = ownership.owners_from(parsed, ["docs/guide.md"])
    assert "docs@acme.example" in owners
    assert "@sam" in owners


@pytest.mark.parametrize("paths", [[], None])
async def test_a_pull_request_that_changes_nothing_is_empty(paths: list[str] | None) -> None:
    outcome = await ownership.gather(FakeProvider(), _pr(), paths or [])
    assert outcome.report.status == "empty"
