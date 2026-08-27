"""Phase 5 — the fix pipeline end to end, against fake providers and models.

What these defend is the boundary the phase exists for: *a failure never writes,
a write never reaches the default branch, and nothing Mira publishes was
merged.*

The providers here are deliberately thin and rude — they refuse permission,
they lose branches, they raise mid-publish — because every one of those is a
real Tuesday and each has to land on the same answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mira.autofix.capabilities import (
    FORGEJO_CAPABILITIES,
    GITHUB_CAPABILITIES,
    GITLAB_CAPABILITIES,
    NO_CAPABILITIES,
    AutofixCapabilities,
)
from mira.autofix.models import ReasonCode, job_key
from mira.autofix.service import FixRequest, request_fix, run_job
from mira.config import AutofixConfig, AutofixRepoPolicy, MiraConfig
from mira.feedback.models import ReviewFinding
from mira.index.store import IndexStore
from mira.models import FileChangeStat

ORIGINAL = "def divide(a, b):\n    return a / b\n"
FIXED = "def divide(a, b):\n    if b == 0:\n        raise ValueError('b must not be zero')\n    return a / b\n"


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    IndexStore.open("acme", "app").close()


def _pr(**overrides: Any) -> SimpleNamespace:
    base = {
        "owner": "acme",
        "repo": "app",
        "number": 7,
        "url": "https://github.com/acme/app/pull/7",
        "title": "Add a divide helper",
        "description": "",
        "base_branch": "main",
        "head_branch": "feature/divide",
        "base_sha": "base123",
        "head_sha": "head456",
        "platform": "github",
        "author": "alice",
        "draft": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _finding(**overrides: Any) -> ReviewFinding:
    base = {
        "id": "11111111-1111-4111-8111-111111111111",
        "fingerprint": "fp1",
        "review_id": 1,
        "platform": "github",
        "owner": "acme",
        "repo": "app",
        "pr_number": 7,
        "pr_url": "https://github.com/acme/app/pull/7",
        "base_sha": "base123",
        "head_sha": "head456",
        "path": "src/math.py",
        "start_line": 2,
        "end_line": 2,
        "symbol": "divide",
        "category": "bug",
        "severity": "blocker",
        "confidence": 0.9,
        "title": "Division by zero is unguarded",
        "body": "`divide` raises ZeroDivisionError when b is 0.",
        "suggestion": "",
        "detector": "llm",
        "prompt_model": "test-model",
        "state": "open",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    base.update(overrides)
    return ReviewFinding(**base)


def _save(finding: ReviewFinding) -> None:
    store = IndexStore.open("acme", "app")
    try:
        store.save_review_finding(finding)
    finally:
        store.close()


def _config(**autofix: Any) -> MiraConfig:
    settings = {"mode": "on", "max_attempts": 2}
    settings.update(autofix)
    return MiraConfig(autofix=AutofixConfig(**settings))


class FakeProvider:
    """A provider that behaves, until a test asks it not to."""

    def __init__(
        self,
        *,
        capabilities: AutofixCapabilities = GITHUB_CAPABILITIES,
        permission: str = "write",
        default_branch: str = "main",
        sources: dict[str, str] | None = None,
        changed: list[str] | None = None,
        is_fork: bool = False,
        raise_on: str = "",
        existing_pr: tuple[int, str] | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._permission = permission
        self._default_branch = default_branch
        self.sources = dict(sources or {"src/math.py": ORIGINAL})
        self._changed = changed or ["src/math.py"]
        self._is_fork = is_fork
        self._raise_on = raise_on
        self._existing_pr = existing_pr
        # What actually happened on the "platform".
        self.branches: dict[str, str] = {"main": "main000", "feature/divide": "head456"}
        self.branch_contents: dict[str, dict[str, str]] = {}
        self.commits: list[tuple[str, str, dict[str, str]]] = []
        self.pulls: list[dict[str, Any]] = []
        self.comments: list[str] = []
        self.replies: list[str] = []
        self.merges: list[str] = []

    def _maybe_raise(self, name: str) -> None:
        if self._raise_on == name:
            raise RuntimeError(f"{name} is unavailable")

    # ── read ──
    def autofix_capabilities(self) -> AutofixCapabilities:
        return self._capabilities

    async def get_pr_info(self, pr_url: str) -> SimpleNamespace:
        return _pr(url=pr_url)

    async def get_actor_permission(self, pr_info: Any, login: str) -> str:
        self._maybe_raise("permission")
        return self._permission

    async def get_default_branch(self, pr_info: Any) -> str:
        self._maybe_raise("default_branch")
        return self._default_branch

    async def get_pr_change_stats(self, pr_info: Any) -> list[FileChangeStat]:
        return [FileChangeStat(path=path, added_lines=4, deleted_lines=1) for path in self._changed]

    async def get_file_content(self, pr_info: Any, path: str, ref: str) -> str:
        return self.sources.get(path, "")

    async def get_pr_diff(self, pr_info: Any) -> str:
        return "diff --git a/src/math.py b/src/math.py\n"

    async def pr_head_is_fork(self, pr_info: Any) -> bool:
        return self._is_fork

    # ── write ──
    async def get_branch_head(self, pr_info: Any, branch: str) -> str:
        self._maybe_raise("branch_head")
        return self.branches.get(branch, "")

    async def create_branch(self, pr_info: Any, branch: str, from_sha: str) -> None:
        self._maybe_raise("create_branch")
        if branch in self.branches:
            raise RuntimeError("branch already exists")
        self.branches[branch] = from_sha
        self.branch_contents[branch] = dict(self.sources)

    async def files_match(self, pr_info: Any, branch: str, files: dict[str, str]) -> bool:
        current = self.branch_contents.get(branch, {})
        return all(current.get(path) == content for path, content in files.items())

    async def commit_files(
        self, pr_info: Any, branch: str, files: dict[str, str], message: str
    ) -> str:
        self._maybe_raise("commit")
        self.branch_contents.setdefault(branch, {}).update(files)
        sha = f"commit{len(self.commits)}"
        self.branches[branch] = sha
        self.commits.append((branch, message, dict(files)))
        return sha

    async def create_pull_request(
        self, pr_info: Any, *, head: str, base: str, title: str, body: str
    ) -> tuple[int, str]:
        self._maybe_raise("create_pr")
        number = 900 + len(self.pulls)
        record = {"head": head, "base": base, "title": title, "body": body, "number": number}
        self.pulls.append(record)
        return number, f"https://github.com/acme/app/pull/{number}"

    async def find_open_pull_request(self, pr_info: Any, head: str) -> tuple[int, str] | None:
        if self._existing_pr is not None:
            return self._existing_pr
        for record in self.pulls:
            if record["head"] == head:
                return record["number"], f"https://github.com/acme/app/pull/{record['number']}"
        return None

    async def post_comment(self, pr_info: Any, body: str) -> None:
        self.comments.append(body)

    async def reply_to_review_comment(self, pr_info: Any, comment_id: int, body: str) -> None:
        self.replies.append(body)


class FakeLLM:
    """Returns a canned `submit_fix` payload, or a scripted sequence of them."""

    def __init__(self, *payloads: dict[str, Any], fail: bool = False) -> None:
        self._payloads = list(payloads) or [
            {
                "edits": [
                    {
                        "path": "src/math.py",
                        "find": "    return a / b",
                        "replace": (
                            "    if b == 0:\n"
                            "        raise ValueError('b must not be zero')\n"
                            "    return a / b"
                        ),
                        "rationale": "guard the divisor",
                    }
                ],
                "summary": "guard divide against a zero divisor",
                "rationale": "divide raised ZeroDivisionError for b == 0.",
                "confidence": 0.9,
            }
        ]
        self._fail = fail
        self.calls: list[list[dict[str, str]]] = []
        self.config = SimpleNamespace(model="test-model")

    async def complete_with_tools(self, messages, tools, temperature=None):  # noqa: ANN001
        self.calls.append(messages)
        if self._fail:
            raise RuntimeError("the model is down")
        payload = self._payloads[min(len(self.calls) - 1, len(self._payloads) - 1)]
        return json.dumps(payload)


async def _accept(provider: FakeProvider, config: MiraConfig, **kwargs: Any):
    return await request_fix(
        provider,
        _pr(),
        FixRequest(actor=kwargs.pop("actor", "alice"), **kwargs),
        config=config,
    )


# ── the happy path ───────────────────────────────────────────────────────────


async def test_a_maintainer_gets_a_reviewable_change_on_its_own_branch() -> None:
    """The acceptance criterion, in one test."""
    _save(_finding())
    provider = FakeProvider()
    config = _config()

    outcome = await _accept(provider, config, finding_id=_finding().id)
    assert outcome.ok, outcome.reasons
    job = outcome.accepted[0]
    assert job.state == "queued"

    store = IndexStore.open("acme", "app")
    try:
        leased = store.claim_autofix_job(worker="w1", lease_seconds=60)
        result = await run_job(provider, leased, config=config, llm=FakeLLM(), store=store)
    finally:
        store.close()

    assert result.job.state == "opened"
    assert result.job.branch_name.startswith("mira/fix/pr-7/")
    assert result.job.child_pr_url.endswith("/900")
    # The change is real, is on Mira's branch, and is not on the default one.
    assert provider.branch_contents[result.job.branch_name]["src/math.py"] == FIXED
    assert "main" not in provider.branch_contents
    assert provider.pulls[0]["base"] == "feature/divide"
    # And nothing was merged, by anybody, ever.
    assert provider.merges == []
    assert not hasattr(provider, "merge")


async def test_the_pull_request_body_carries_the_evidence() -> None:
    _save(_finding())
    provider = FakeProvider()
    config = _config()
    await _accept(provider, config, finding_id=_finding().id)
    store = IndexStore.open("acme", "app")
    try:
        job = store.claim_autofix_job(worker="w1", lease_seconds=60)
        await run_job(provider, job, config=config, llm=FakeLLM(), store=store)
    finally:
        store.close()

    body = provider.pulls[0]["body"]
    assert _finding().id in body
    assert "test-model" in body
    assert "@alice" in body
    assert "syntax" in body  # the validation table
    assert "A model wrote this" in body


# ── authorization ────────────────────────────────────────────────────────────


async def test_a_reader_cannot_make_mira_create_a_branch() -> None:
    _save(_finding())
    provider = FakeProvider(permission="read")
    outcome = await _accept(provider, _config(), finding_id=_finding().id)
    assert not outcome.ok
    assert outcome.reasons[0].code == ReasonCode.ACTOR_LACKS_WRITE
    assert provider.branches == {"main": "main000", "feature/divide": "head456"}
    assert provider.commits == []
    assert provider.pulls == []


async def test_an_unreadable_permission_is_not_a_permission() -> None:
    _save(_finding())
    provider = FakeProvider(raise_on="permission")
    outcome = await _accept(provider, _config(), finding_id=_finding().id)
    assert not outcome.ok
    assert outcome.reasons[0].code == ReasonCode.PERMISSION_UNREADABLE
    assert provider.commits == []


async def test_a_provider_that_cannot_report_permissions_refuses() -> None:
    _save(_finding())
    provider = FakeProvider(
        capabilities=AutofixCapabilities(
            provider="github",
            can_create_branch=True,
            can_commit=True,
            can_open_pull_request=True,
            can_read_default_branch=True,
        )
    )
    outcome = await _accept(provider, _config(), finding_id=_finding().id)
    assert not outcome.ok
    assert outcome.reasons[0].code == ReasonCode.PERMISSION_UNREADABLE


async def test_a_blocked_requester_is_refused_without_asking_the_platform() -> None:
    _save(_finding())
    provider = FakeProvider(raise_on="permission")
    config = _config(blocked_requesters=["mallory"])
    outcome = await request_fix(
        provider,
        _pr(),
        FixRequest(actor="mallory", finding_id=_finding().id),
        config=config,
    )
    assert outcome.reasons[0].code == ReasonCode.ACTOR_NOT_ALLOWED


async def test_a_requester_allowlist_narrows_further() -> None:
    _save(_finding())
    provider = FakeProvider()
    config = _config(allowed_requesters=["bob"])
    outcome = await _accept(provider, config, finding_id=_finding().id, actor="alice")
    assert outcome.reasons[0].code == ReasonCode.ACTOR_NOT_ALLOWED


# ── off by default, and the kill switch ──────────────────────────────────────


async def test_autofix_is_off_by_default() -> None:
    _save(_finding())
    provider = FakeProvider()
    outcome = await _accept(provider, MiraConfig(), finding_id=_finding().id)
    assert not outcome.ok
    assert outcome.reasons[0].code == ReasonCode.AUTOFIX_OFF
    assert provider.commits == []


async def test_the_kill_switch_beats_every_per_repo_opt_in() -> None:
    _save(_finding())
    provider = FakeProvider()
    config = MiraConfig(
        autofix=AutofixConfig(
            mode="on",
            kill_switch=True,
            repositories={"acme/app": AutofixRepoPolicy(enabled=True, mode="on")},
        )
    )
    outcome = await _accept(provider, config, finding_id=_finding().id)
    assert outcome.reasons[0].code == ReasonCode.KILL_SWITCH


async def test_suggest_mode_generates_and_validates_but_writes_nothing() -> None:
    _save(_finding())
    provider = FakeProvider()
    config = _config(mode="suggest")
    outcome = await _accept(provider, config, finding_id=_finding().id)
    assert outcome.ok
    store = IndexStore.open("acme", "app")
    try:
        job = store.claim_autofix_job(worker="w1", lease_seconds=60)
        result = await run_job(provider, job, config=config, llm=FakeLLM(), store=store)
    finally:
        store.close()
    assert result.job.state == "opened"
    assert ReasonCode.SUGGEST_ONLY in result.job.reason_codes()
    assert result.job.diff  # the reviewer still gets the patch…
    assert provider.commits == []  # …and the repository is untouched.
    assert provider.pulls == []
    assert provider.branch_contents == {}


# ── the default branch, and force pushes ─────────────────────────────────────


async def test_a_pr_whose_head_is_the_default_branch_is_never_committed_to() -> None:
    """The one shape where "the PR branch" and "the default branch" coincide."""
    _save(_finding())
    provider = FakeProvider()
    config = _config(allow_commit_to_pr_branch=True)
    pr = _pr(head_branch="main")
    outcome = await request_fix(
        provider,
        pr,
        FixRequest(actor="alice", finding_id=_finding().id, mode="pr_branch"),
        config=config,
    )
    assert outcome.ok
    store = IndexStore.open("acme", "app")
    try:
        job = store.claim_autofix_job(worker="w1", lease_seconds=60)
        job.head_branch = "main"
        result = await run_job(provider, job, config=config, llm=FakeLLM(), store=store)
    finally:
        store.close()
    assert result.job.state == "dead_letter"
    assert ReasonCode.DEFAULT_BRANCH_REFUSED in result.job.reason_codes()
    assert provider.commits == []


async def test_a_fork_head_is_never_committed_to() -> None:
    _save(_finding())
    provider = FakeProvider(is_fork=True)
    config = _config(allow_commit_to_pr_branch=True)
    outcome = await request_fix(
        provider,
        _pr(),
        FixRequest(actor="alice", finding_id=_finding().id, mode="pr_branch"),
        config=config,
    )
    assert outcome.ok
    store = IndexStore.open("acme", "app")
    try:
        job = store.claim_autofix_job(worker="w1", lease_seconds=60)
        result = await run_job(provider, job, config=config, llm=FakeLLM(), store=store)
    finally:
        store.close()
    assert result.job.state == "dead_letter"
    assert ReasonCode.FORK_HEAD_REFUSED in result.job.reason_codes()
    assert provider.commits == []


async def test_committing_to_the_pr_branch_needs_an_explicit_opt_in() -> None:
    _save(_finding())
    provider = FakeProvider()
    outcome = await request_fix(
        provider,
        _pr(),
        FixRequest(actor="alice", finding_id=_finding().id, mode="pr_branch"),
        config=_config(),
    )
    assert not outcome.ok
    assert outcome.reasons[0].code == ReasonCode.MODE_NOT_PERMITTED


async def test_a_provider_that_cannot_name_the_default_branch_never_writes() -> None:
    _save(_finding())
    provider = FakeProvider(default_branch="")
    config = _config()
    await _accept(provider, config, finding_id=_finding().id)
    store = IndexStore.open("acme", "app")
    try:
        job = store.claim_autofix_job(worker="w1", lease_seconds=60)
        result = await run_job(provider, job, config=config, llm=FakeLLM(), store=store)
    finally:
        store.close()
    assert result.job.state != "opened"
    assert provider.commits == []
    assert provider.pulls == []


# ── idempotency ──────────────────────────────────────────────────────────────


async def test_asking_twice_produces_one_job() -> None:
    _save(_finding())
    provider = FakeProvider()
    config = _config()
    first = await _accept(provider, config, finding_id=_finding().id)
    second = await _accept(provider, config, finding_id=_finding().id, actor="bob")
    assert first.accepted[0].job_key == second.accepted[0].job_key
    assert first.accepted[0].id == second.accepted[0].id


async def test_a_retried_publish_reuses_the_branch_the_commit_and_the_pull_request() -> None:
    """The crash-and-come-back case: nothing is duplicated on the second run."""
    _save(_finding())
    provider = FakeProvider()
    config = _config(max_attempts=3)
    await _accept(provider, config, finding_id=_finding().id)

    store = IndexStore.open("acme", "app")
    try:
        job = store.claim_autofix_job(worker="w1", lease_seconds=60)
        first = await run_job(provider, job, config=config, llm=FakeLLM(), store=store)
        assert first.job.state == "opened"

        # A retry of the *same* work: same key, same head, same everything.
        store.update_autofix_job(job.job_key, state="queued", available_at=0)
        again = store.claim_autofix_job(worker="w2", lease_seconds=60)
        second = await run_job(provider, again, config=config, llm=FakeLLM(), store=store)
    finally:
        store.close()

    assert second.job.state == "opened"
    assert len(provider.pulls) == 1
    assert len(provider.commits) == 1  # the second publish saw its own content
    assert second.job.branch_name == first.job.branch_name
    assert second.job.child_pr_url == first.job.child_pr_url


def test_branch_names_are_deterministic_sanitized_and_collision_free() -> None:
    from mira.autofix.models import branch_name

    first = branch_name(prefix="mira/fix", pr_number=7, finding_id="abc-123", title="Fix ../../etc")
    again = branch_name(prefix="mira/fix", pr_number=7, finding_id="abc-123", title="Fix ../../etc")
    other = branch_name(prefix="mira/fix", pr_number=7, finding_id="abc-124", title="Fix ../../etc")
    assert first == again
    assert first != other
    assert ".." not in first
    assert first.startswith("mira/fix/pr-7/")
    assert all(char.isalnum() or char in "-/" for char in first)


def test_a_hostile_title_cannot_escape_the_branch_name() -> None:
    from mira.autofix.models import branch_name

    hostile = branch_name(
        prefix="mira/fix",
        pr_number=1,
        finding_id="f" * 32,
        title="../../../refs/heads/main\x00 --force $(rm -rf /)",
    )
    assert hostile.startswith("mira/fix/pr-1/")
    assert ".." not in hostile and "\x00" not in hostile and "$" not in hostile


# ── fix all ──────────────────────────────────────────────────────────────────


async def test_fix_all_respects_its_limit_and_names_what_it_left_out() -> None:
    for index in range(5):
        _save(
            _finding(
                id=f"{index}1111111-1111-4111-8111-11111111111{index}",
                title=f"Finding {index}",
                severity="blocker",
                created_at=float(index),
            )
        )
    provider = FakeProvider()
    config = _config(max_fixes_per_request=2, max_concurrent_jobs=10)
    outcome = await _accept(provider, config, kind="all")
    assert len(outcome.accepted) == 2
    assert len(outcome.skipped) == 3
    assert all(reason.code == ReasonCode.REQUEST_LIMIT for _finding_id, reason in outcome.skipped)


async def test_fix_all_never_silently_includes_low_severity_findings() -> None:
    _save(_finding(id="a1111111-1111-4111-8111-111111111111", severity="nitpick"))
    _save(_finding(id="b1111111-1111-4111-8111-111111111111", severity="blocker"))
    provider = FakeProvider()
    outcome = await _accept(provider, _config(), kind="all")
    assert len(outcome.accepted) == 1
    assert outcome.accepted[0].finding_id.startswith("b")
    assert outcome.skipped[0][0].startswith("a")


async def test_fix_all_respects_the_concurrency_ceiling() -> None:
    for index in range(4):
        _save(_finding(id=f"{index}1111111-1111-4111-8111-11111111111{index}", severity="blocker"))
    provider = FakeProvider()
    config = _config(max_fixes_per_request=4, max_concurrent_jobs=2)
    outcome = await _accept(provider, config, kind="all")
    assert len(outcome.accepted) == 2
    assert any(reason.code == ReasonCode.CONCURRENCY_LIMIT for _f, reason in outcome.skipped)


async def test_a_second_request_is_refused_once_the_queue_is_full() -> None:
    _save(_finding())
    _save(_finding(id="c1111111-1111-4111-8111-111111111111"))
    provider = FakeProvider()
    config = _config(max_concurrent_jobs=1)
    first = await _accept(provider, config, finding_id=_finding().id)
    assert first.ok
    second = await _accept(provider, config, finding_id="c1111111-1111-4111-8111-111111111111")
    assert not second.ok
    assert second.reasons[0].code == ReasonCode.CONCURRENCY_LIMIT


# ── provenance ───────────────────────────────────────────────────────────────


async def test_a_finding_from_another_pull_request_is_refused() -> None:
    _save(_finding(pr_number=99))
    provider = FakeProvider()
    outcome = await _accept(provider, _config(), finding_id=_finding().id)
    assert outcome.reasons[0].code == ReasonCode.FINDING_OTHER_PR


async def test_an_unknown_finding_id_is_refused() -> None:
    provider = FakeProvider()
    outcome = await _accept(provider, _config(), finding_id="not-a-finding")
    assert outcome.reasons[0].code == ReasonCode.FINDING_NOT_FOUND


async def test_a_resolved_finding_is_not_fixed_again() -> None:
    _save(_finding(state="resolved"))
    provider = FakeProvider()
    outcome = await _accept(provider, _config(), finding_id=_finding().id)
    assert outcome.reasons[0].code == ReasonCode.FINDING_NOT_OPEN


def test_the_job_key_is_the_finding_and_the_commit_not_the_requester() -> None:
    common = {
        "platform": "github",
        "owner": "acme",
        "repo": "app",
        "pr_number": 7,
        "mode": "branch_pr",
    }
    same = job_key(head_sha="a", finding_id="f1", **common)
    assert same == job_key(head_sha="a", finding_id="f1", **common)
    assert same != job_key(head_sha="b", finding_id="f1", **common)
    assert same != job_key(head_sha="a", finding_id="f2", **common)


# ── failure handling ─────────────────────────────────────────────────────────


async def test_a_model_failure_is_retried_then_dead_lettered() -> None:
    _save(_finding())
    provider = FakeProvider()
    config = _config(max_attempts=2, retry_backoff_seconds=0)
    await _accept(provider, config, finding_id=_finding().id)
    store = IndexStore.open("acme", "app")
    try:
        first = store.claim_autofix_job(worker="w1", lease_seconds=60)
        one = await run_job(provider, first, config=config, llm=FakeLLM(fail=True), store=store)
        assert one.job.state == "failed"

        second = store.claim_autofix_job(worker="w1", lease_seconds=60)
        two = await run_job(provider, second, config=config, llm=FakeLLM(fail=True), store=store)
        assert two.job.state == "dead_letter"
        assert ReasonCode.ATTEMPT_LIMIT in two.job.reason_codes()

        assert store.claim_autofix_job(worker="w1", lease_seconds=60) is None
    finally:
        store.close()
    assert provider.commits == []


async def test_a_publish_failure_leaves_no_pull_request_behind() -> None:
    _save(_finding())
    provider = FakeProvider(raise_on="create_pr")
    config = _config(max_attempts=1)
    await _accept(provider, config, finding_id=_finding().id)
    store = IndexStore.open("acme", "app")
    try:
        job = store.claim_autofix_job(worker="w1", lease_seconds=60)
        result = await run_job(provider, job, config=config, llm=FakeLLM(), store=store)
    finally:
        store.close()
    assert result.job.state == "dead_letter"
    assert ReasonCode.PUBLISH_FAILED in result.job.reason_codes()
    assert provider.pulls == []


async def test_every_attempt_is_auditable() -> None:
    _save(_finding())
    provider = FakeProvider()
    config = _config()
    await _accept(provider, config, finding_id=_finding().id)
    store = IndexStore.open("acme", "app")
    try:
        job = store.claim_autofix_job(worker="w1", lease_seconds=60)
        await run_job(provider, job, config=config, llm=FakeLLM(), store=store)
        attempts = store.list_autofix_attempts(job_key=job.job_key)
    finally:
        store.close()
    phases = [attempt.phase for attempt in attempts]
    assert phases == ["generate", "apply", "validate", "publish"]
    assert any(attempt.diff for attempt in attempts)
    assert attempts[-1].outcome == "opened"


# ── provider parity ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "capabilities",
    [GITHUB_CAPABILITIES, GITLAB_CAPABILITIES, FORGEJO_CAPABILITIES],
    ids=["github", "gitlab", "forgejo"],
)
async def test_every_supported_provider_publishes_the_same_way(
    capabilities: AutofixCapabilities,
) -> None:
    _save(_finding())
    provider = FakeProvider(capabilities=capabilities)
    config = _config()
    outcome = await _accept(provider, config, finding_id=_finding().id)
    assert outcome.ok
    store = IndexStore.open("acme", "app")
    try:
        job = store.claim_autofix_job(worker="w1", lease_seconds=60)
        result = await run_job(provider, job, config=config, llm=FakeLLM(), store=store)
    finally:
        store.close()
    assert result.job.state == "opened"
    assert provider.branch_contents[result.job.branch_name]["src/math.py"] == FIXED


async def test_a_provider_that_cannot_write_degrades_explicitly() -> None:
    _save(_finding())
    provider = FakeProvider(capabilities=NO_CAPABILITIES)
    outcome = await _accept(provider, _config(), finding_id=_finding().id)
    assert not outcome.ok
    assert outcome.reasons[0].code == ReasonCode.PROVIDER_CANNOT_WRITE
    assert "cannot" in outcome.reasons[0].message


def test_no_provider_declares_that_mira_may_merge() -> None:
    for capabilities in (
        GITHUB_CAPABILITIES,
        GITLAB_CAPABILITIES,
        FORGEJO_CAPABILITIES,
        NO_CAPABILITIES,
    ):
        assert capabilities.can_merge is False


def test_a_provider_cannot_widen_its_platform() -> None:
    from mira.autofix.capabilities import narrow

    liar = AutofixCapabilities(provider="gitlab", can_merge=True, can_create_branch=True)
    narrowed = narrow(liar, GITLAB_CAPABILITIES)
    assert narrowed.can_merge is False


# ── the whole pipeline never merges ──────────────────────────────────────────


def test_the_word_merge_appears_in_no_write_path() -> None:
    """A grep as a test, because the guarantee is an absence.

    An absence cannot be asserted by exercising code — there is no call to
    observe not happening — so it is asserted against the source. If somebody
    later adds a merge call to the publish path, this fails and they have to
    say why in a review.
    """
    import mira.autofix.publish as publish_module

    source = Path(publish_module.__file__).read_text(encoding="utf-8")
    lines = [
        line
        for line in source.splitlines()
        if ".merge" in line or "merge_pull" in line or "merge_when" in line
    ]
    assert lines == [], lines


# ── validation blocks publication ────────────────────────────────────────────


async def test_a_failing_formatter_stops_the_fix_before_anything_is_written() -> None:
    """End to end: the check fails, and no branch, commit or pull request exists."""
    import sys

    from mira.config import AutofixValidationConfig

    _save(_finding())
    provider = FakeProvider()
    config = _config(
        max_attempts=1,
        validation=AutofixValidationConfig(
            commands=[{"name": "format", "command": [sys.executable, "-c", "raise SystemExit(1)"]}]
        ),
    )
    await _accept(provider, config, finding_id=_finding().id)
    store = IndexStore.open("acme", "app")
    try:
        job = store.claim_autofix_job(worker="w1", lease_seconds=60)
        result = await run_job(provider, job, config=config, llm=FakeLLM(), store=store)
    finally:
        store.close()

    assert result.job.state == "dead_letter"
    assert ReasonCode.VALIDATION_FAILED in result.job.reason_codes()
    assert provider.branches == {"main": "main000", "feature/divide": "head456"}
    assert provider.commits == []
    assert provider.pulls == []
    # And the evidence is attached, so a human can see what the check said.
    assert [check.name for check in result.job.validation.failures] == ["format"]


async def test_a_patch_that_does_not_parse_never_reaches_a_branch() -> None:
    _save(_finding())
    provider = FakeProvider()
    config = _config(max_attempts=1)
    broken = FakeLLM(
        {
            "edits": [
                {
                    "path": "src/math.py",
                    "find": "    return a / b",
                    "replace": "    if b == 0",  # no colon, no body
                }
            ],
            "summary": "break it",
        }
    )
    await _accept(provider, config, finding_id=_finding().id)
    store = IndexStore.open("acme", "app")
    try:
        job = store.claim_autofix_job(worker="w1", lease_seconds=60)
        result = await run_job(provider, job, config=config, llm=broken, store=store)
    finally:
        store.close()
    assert result.job.state == "dead_letter"
    assert provider.commits == []


async def test_a_failed_validation_feeds_the_next_attempt_what_it_said() -> None:
    """The retry is told what the tool objected to, as data."""
    import sys

    from mira.config import AutofixValidationConfig

    _save(_finding())
    provider = FakeProvider()
    config = _config(
        max_attempts=2,
        retry_backoff_seconds=0,
        validation=AutofixValidationConfig(
            commands=[
                {
                    "name": "format",
                    "command": [
                        sys.executable,
                        "-c",
                        "import sys; sys.stderr.write('E501 line too long'); sys.exit(1)",
                    ],
                }
            ]
        ),
    )
    await _accept(provider, config, finding_id=_finding().id)
    llm = FakeLLM()
    store = IndexStore.open("acme", "app")
    try:
        first = store.claim_autofix_job(worker="w1", lease_seconds=60)
        await run_job(provider, first, config=config, llm=llm, store=store)
        second = store.claim_autofix_job(worker="w1", lease_seconds=60)
        assert second is not None
        await run_job(provider, second, config=config, llm=llm, store=store)
    finally:
        store.close()

    retry_prompt = llm.calls[-1][1]["content"]
    assert "previous attempt was rejected" in retry_prompt
    assert "E501 line too long" in retry_prompt
    assert provider.commits == []


async def test_a_protected_path_is_refused_without_burning_the_retry_budget() -> None:
    """Retrying a refusal that is a property of the request only delays telling
    somebody."""
    _save(_finding())
    provider = FakeProvider(
        sources={".github/workflows/ci.yml": "on: push\n"},
        changed=[".github/workflows/ci.yml"],
    )
    config = _config(max_attempts=3)
    llm = FakeLLM(
        {
            "edits": [
                {
                    "path": ".github/workflows/ci.yml",
                    "find": "on: push",
                    "replace": "on: [push, pull_request]",
                }
            ],
            "summary": "widen the trigger",
        }
    )
    await _accept(provider, config, finding_id=_finding().id)
    store = IndexStore.open("acme", "app")
    try:
        job = store.claim_autofix_job(worker="w1", lease_seconds=60)
        result = await run_job(provider, job, config=config, llm=llm, store=store)
    finally:
        store.close()
    assert result.job.state == "dead_letter"  # not `failed`, on the first try
    assert result.job.attempts == 1
    assert ReasonCode.PATH_PROTECTED in result.job.reason_codes()
