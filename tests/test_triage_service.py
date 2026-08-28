"""Phase 7C — the service: gather, rank, persist, announce, never raise.

The properties that matter here are the ones a reader of a pull request depends
on.

*A suggestion is never an assignment.* No code path in this package calls a
provider method that requests a review or adds an assignee. A fake provider
records every method the service touched, and the test asserts the set contains
nothing that could.

*"Nobody" and "we could not tell" reach the comment differently.* A run with no
candidates and a failed signal is ``unavailable`` and says so in Mira's own
name; a run with no candidates and every signal answered is ``no_candidates``.

*Triage off means nothing happens.* Not "runs and posts nothing" — nothing
fetched, nothing written, nobody's name recorded.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from mira.config import MiraConfig, TriageConfig
from mira.models import FileChangeStat, PathAuthorship, PRInfo
from mira.triage import service as triage_service
from mira.triage.explain import public_explanation
from mira.triage.policy import resolve_policy

OWNERS = "src/ @dana\ndocs/ @sam\n"

DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,0 +1,2 @@\n"
    "+import os\n"
    "+x = 1\n"
)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture(autouse=True)
def no_review_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests do not care about load; the ones that do set it themselves."""
    from mira.triage import load as load_module

    monkeypatch.setattr(load_module, "current", lambda **_: load_module.LoadOutcome())


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
        "author": "kit",
        "base_sha": "base111",
        "head_sha": "head222",
    }
    fields.update(overrides)
    return PRInfo(**fields)


def _returning(value: Any):  # type: ignore[no-untyped-def]
    """A stand-in for a provider coroutine that always returns ``value``."""

    async def _call(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return _call


def _config(**overrides: Any) -> MiraConfig:
    config = MiraConfig()
    config.triage = TriageConfig(**{"enabled": True, **overrides})
    return config


class FakeProvider:
    """A provider that records every call, so a test can assert on the set."""

    def __init__(
        self,
        *,
        owners: str | None = OWNERS,
        owners_error: Exception | None = None,
        authors: dict[str, list[PathAuthorship]] | None = None,
        existing_comment: int | None = None,
        capabilities: Any = None,
    ) -> None:
        self.owners = owners
        self.owners_error = owners_error
        self.authors = authors or {}
        self.existing_comment = existing_comment
        self.calls: list[str] = []
        self.posted: list[str] = []
        self.updated: list[tuple[int, str]] = []
        self._capabilities = capabilities

    def triage_capabilities(self) -> Any:
        from mira.triage.capabilities import GITHUB_CAPABILITIES

        return self._capabilities or GITHUB_CAPABILITIES

    async def get_pr_change_stats(self, pr_info: PRInfo) -> list[FileChangeStat]:
        self.calls.append("get_pr_change_stats")
        return [FileChangeStat(path="src/app.py", added_lines=2)]

    async def get_codeowners(self, pr_info: PRInfo, ref: str = "") -> tuple[str, str]:
        self.calls.append("get_codeowners")
        if self.owners_error is not None:
            raise self.owners_error
        if self.owners is None:
            return "", ""
        return ".github/CODEOWNERS", self.owners

    async def get_path_authors(
        self, pr_info: PRInfo, paths: list[str], *, ref: str = "", max_per_path: int = 20
    ) -> dict[str, list[PathAuthorship]]:
        self.calls.append("get_path_authors")
        return self.authors

    async def find_bot_comment(self, pr_info: PRInfo, marker: str) -> int | None:
        self.calls.append("find_bot_comment")
        return self.existing_comment

    async def post_comment(self, pr_info: PRInfo, body: str) -> None:
        self.calls.append("post_comment")
        self.posted.append(body)

    async def update_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        self.calls.append("update_comment")
        self.updated.append((comment_id, body))


async def test_a_run_suggests_the_owner_and_records_it() -> None:
    provider = FakeProvider()
    run = await triage_service.evaluate(provider, _pr(), config=_config())

    assert run.status == "ok"
    assert run.suggested == ["dana"]
    assert run.classification.size == "xs"
    assert run.inputs.ownership_ref == "base111"

    stored = triage_service.latest_for("acme", "app", "github", 7, "head222")
    assert stored is not None
    assert stored.suggested == ["dana"]


async def test_nothing_at_all_happens_when_triage_is_off() -> None:
    provider = FakeProvider()
    config = MiraConfig()
    run = await triage_service.evaluate(provider, _pr(), config=config)

    assert run.status == "not_run"
    assert provider.calls == []
    assert triage_service.latest_for("acme", "app", "github", 7, "head222") is None


async def test_the_kill_switch_stops_it_everywhere() -> None:
    provider = FakeProvider()
    run = await triage_service.evaluate(provider, _pr(), config=_config(kill_switch=True))
    assert run.status == "not_run"
    assert provider.calls == []


async def test_nobody_to_suggest_is_an_answer() -> None:
    """Every signal answered and produced nobody. That is `no_candidates`."""
    provider = FakeProvider(owners=None)
    run = await triage_service.evaluate(provider, _pr(), config=_config())

    assert run.status == "no_candidates"
    assert run.degraded is False
    body = public_explanation(run)
    assert "found nobody to suggest" in body
    assert "That is the answer, not a failure" in body


async def test_a_broken_lookup_is_never_rendered_as_nobody_available() -> None:
    provider = FakeProvider(owners_error=RuntimeError("503 from the API"))
    run = await triage_service.evaluate(provider, _pr(), config=_config())

    assert run.status == "unavailable"
    body = public_explanation(run)
    assert "could not work out who to suggest" in body
    assert "problem with Mira, not with this pull request" in body
    assert "503" in body


async def test_a_degraded_signal_is_admitted_even_when_somebody_was_found() -> None:
    """A short list built on half the evidence should say which half."""
    provider = FakeProvider(owners_error=RuntimeError("gateway timeout"))
    store_rows = [
        {
            "platform": "github",
            "path": "src/app.py",
            "identity": "ari",
            "role": "authored",
            "source": "pull_request",
            "reference": "3",
            "event_at": 1e12,
        }
    ]
    from mira.index.store import IndexStore

    store = IndexStore.open("acme", "app")
    store.record_path_contributions(store_rows)
    store.close()

    run = await triage_service.evaluate(provider, _pr(), config=_config())
    assert run.status == "ok"
    assert run.degraded is True
    assert "may be short" in public_explanation(run)


async def test_the_author_of_the_pull_request_is_never_suggested() -> None:
    provider = FakeProvider(owners="src/ @kit\n")
    run = await triage_service.evaluate(provider, _pr(author="kit"), config=_config())
    assert run.status == "no_candidates"
    assert [(e.identity, e.reason) for e in run.excluded] == [("kit", "author")]


async def test_the_service_never_asks_anybody_to_review() -> None:
    """The invariant of the phase, asserted against the provider surface.

    Not "we did not mean to assign" — the set of methods the service actually
    called, checked against every spelling a review request has on the three
    platforms.
    """
    provider = FakeProvider()
    await triage_service.evaluate(provider, _pr(), config=_config())

    forbidden = {
        "request_reviewers",
        "add_assignee",
        "add_assignees",
        "create_review_request",
        "submit_verdict",
        "post_review",
        "add_label",
        "publish_gate_status",
        "publish_checks_status",
    }
    assert forbidden.isdisjoint(set(provider.calls))


async def test_the_comment_is_updated_in_place_rather_than_stacked() -> None:
    provider = FakeProvider(existing_comment=42)
    await triage_service.evaluate(provider, _pr(), config=_config())
    assert provider.posted == []
    assert provider.updated and provider.updated[0][0] == 42


async def test_a_repository_with_nobody_to_suggest_does_not_collect_a_comment() -> None:
    """Creating one only for a run that has something to say.

    A repository where triage can never find anybody should not grow a comment
    on every pull request saying so.
    """
    provider = FakeProvider(owners=None)
    run = await triage_service.evaluate(provider, _pr(), config=_config())
    assert run.status == "no_candidates"
    assert provider.posted == []


async def test_a_stale_suggestion_is_updated_when_the_new_run_finds_nobody() -> None:
    provider = FakeProvider(owners=None, existing_comment=42)
    await triage_service.evaluate(provider, _pr(), config=_config())
    assert provider.updated and "found nobody" in provider.updated[0][1]


async def test_a_draft_is_recorded_and_not_announced() -> None:
    provider = FakeProvider()
    run = await triage_service.evaluate(provider, _pr(draft=True), config=_config())
    assert run.status == "ok"
    assert provider.posted == []
    assert provider.updated == []
    assert triage_service.latest_for("acme", "app", "github", 7, "head222") is not None


async def test_comments_can_be_switched_off_without_switching_triage_off() -> None:
    provider = FakeProvider()
    run = await triage_service.evaluate(provider, _pr(), config=_config(comment=False))
    assert run.status == "ok"
    assert "find_bot_comment" not in provider.calls


async def test_announcing_can_be_suppressed_for_a_dry_run() -> None:
    provider = FakeProvider()
    await triage_service.evaluate(provider, _pr(), config=_config(), announce_result=False)
    assert provider.posted == []
    assert "find_bot_comment" not in provider.calls


async def test_the_same_pull_request_at_the_same_commit_is_one_run() -> None:
    provider = FakeProvider()
    first = await triage_service.evaluate(provider, _pr(), config=_config())
    second = await triage_service.evaluate(provider, _pr(), config=_config())
    assert first.run_key == second.run_key
    assert second.attempts == 2

    from mira.index.store import IndexStore

    store = IndexStore.open("acme", "app")
    assert store.count_triage_runs({}) == 1
    store.close()


async def test_a_push_produces_a_new_run() -> None:
    provider = FakeProvider()
    first = await triage_service.evaluate(provider, _pr(), config=_config())
    second = await triage_service.evaluate(provider, _pr(head_sha="head333"), config=_config())
    assert first.run_key != second.run_key


async def test_a_policy_change_produces_a_new_run() -> None:
    provider = FakeProvider()
    first = await triage_service.evaluate(provider, _pr(), config=_config())
    second = await triage_service.evaluate(provider, _pr(), config=_config(max_suggestions=1))
    assert first.run_key != second.run_key


async def test_a_diff_that_cannot_be_read_is_unavailable_not_empty() -> None:
    class Broken(FakeProvider):
        async def get_pr_change_stats(self, pr_info: PRInfo) -> list[FileChangeStat]:
            raise RuntimeError("the API is down")

    run = await triage_service.evaluate(Broken(), _pr(), config=_config())
    assert run.status == "unavailable"
    assert "the changed files could not be read" in run.error


async def test_the_review_hands_over_its_diff_rather_than_refetching() -> None:
    provider = FakeProvider()
    run = await triage_service.evaluate(
        provider,
        _pr(),
        config=_config(),
        signal=triage_service.ReviewSignal(diff_text=DIFF, review_id=11),
    )
    assert "get_pr_change_stats" not in provider.calls
    assert run.inputs.changed_paths == ["src/app.py"]
    assert run.inputs.review_id == 11


async def test_a_budget_that_runs_out_leaves_every_signal_unanswered() -> None:
    class Slow(FakeProvider):
        async def get_codeowners(self, pr_info: PRInfo, ref: str = "") -> tuple[str, str]:
            await asyncio.sleep(5)
            return ".github/CODEOWNERS", OWNERS

    run = await triage_service.evaluate(Slow(), _pr(), config=_config(budget_seconds=1.0))
    assert run.status == "unavailable"
    assert all(not report.answered for report in run.signals)
    assert "budget ran out" in run.signals[0].detail


async def test_an_unreadable_review_load_is_admitted_rather_than_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mira.triage import load as load_module

    monkeypatch.setattr(
        load_module,
        "current",
        lambda **_: load_module.LoadOutcome(available=False, detail="no review database"),
    )
    run = await triage_service.evaluate(FakeProvider(), _pr(), config=_config())
    assert run.status == "ok"
    assert any("Review load could not be read" in note for note in run.notes)
    assert "Review load could not be read" in public_explanation(run)


async def test_the_policy_is_resolved_per_repository() -> None:
    config = _config(enabled=False, repositories={"acme/app": {"enabled": True}})
    assert resolve_policy(config.triage, "acme", "app").active is True
    assert resolve_policy(config.triage, "acme", "other").active is False


# ─────────────────────────────────── the webhook re-evaluation path ──


async def test_a_repository_with_only_triage_on_still_re_evaluates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate being off must not stop a draft-marked-ready from being triaged."""
    from mira.platforms import handlers

    config = _config()
    monkeypatch.setattr(handlers, "load_config", lambda: config)
    provider = FakeProvider()
    provider.get_pr_info = _returning(_pr())  # type: ignore[attr-defined]

    await handlers.run_gate_evaluation(
        provider, "acme", "app", 7, "https://github.com/acme/app/pull/7", "mira-bot"
    )
    assert triage_service.latest_for("acme", "app", "github", 7, "head222") is not None


async def test_a_commit_that_already_has_an_answer_is_not_re_ranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every CI completion wakes this handler; re-ranking the same files under
    the same policy would spend API calls to reach the row already there."""
    from mira.platforms import handlers

    config = _config()
    monkeypatch.setattr(handlers, "load_config", lambda: config)
    provider = FakeProvider()
    provider.get_pr_info = _returning(_pr())  # type: ignore[attr-defined]

    await triage_service.evaluate(provider, _pr(), config=config, announce_result=False)
    provider.calls.clear()

    await handlers.run_gate_evaluation(
        provider, "acme", "app", 7, "https://github.com/acme/app/pull/7", "mira-bot"
    )
    assert "get_codeowners" not in provider.calls


async def test_a_commit_whose_run_could_not_answer_is_tried_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outage gets a free retry on the next event; an answer does not."""
    from mira.platforms import handlers

    config = _config()
    monkeypatch.setattr(handlers, "load_config", lambda: config)
    broken = FakeProvider(owners_error=RuntimeError("502"))
    broken.get_pr_info = _returning(_pr())  # type: ignore[attr-defined]

    failed = await triage_service.evaluate(broken, _pr(), config=config, announce_result=False)
    assert failed.status == "unavailable"
    broken.calls.clear()

    await handlers.run_gate_evaluation(
        broken, "acme", "app", 7, "https://github.com/acme/app/pull/7", "mira-bot"
    )
    assert "get_codeowners" in broken.calls


async def test_a_branch_that_rewrites_codeowners_does_not_get_ranked_under_it() -> None:
    """End to end, against a provider that answers differently per ref.

    The attack is one commit: add a line naming an account you control and be
    suggested as the reviewer of your own change. The base is what is read, so
    the name that comes back is the one the *repository* declared.
    """

    class TwoRefProvider(FakeProvider):
        async def get_codeowners(self, pr_info: PRInfo, ref: str = "") -> tuple[str, str]:
            self.calls.append("get_codeowners")
            if ref == "head222":
                return ".github/CODEOWNERS", "src/ @attacker\n"
            return ".github/CODEOWNERS", "src/ @dana\n"

    provider = TwoRefProvider()
    run = await triage_service.evaluate(provider, _pr(), config=_config())
    assert run.suggested == ["dana"]
    assert "attacker" not in str(run.as_dict())
