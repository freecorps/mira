"""Review coverage, dependency ownership, continuation and retry regressions."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.config import MiraConfig
from mira.core.chunker import chunk_files
from mira.core.dependencies import discover_dependencies
from mira.core.engine import ReviewEngine
from mira.models import FileChangeType, FileDiff, HunkInfo, ReviewChunk


def file(path, source="", language="typescript"):
    return FileDiff(
        path, FileChangeType.MODIFIED, [HunkInfo(1, 1, 1, 1, source)], language=language
    )


def diff(paths, lines=1):
    return "".join(
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{lines} @@\n" + "".join(f"+value_{i} = {i}\n" for i in range(lines))
        for path in paths
    )


def engine_config():
    cfg = MiraConfig()
    cfg.review.walkthrough = False
    cfg.review.security_pass = False
    cfg.review.dependency_overlap = False
    cfg.review.self_critique = False
    cfg.review.include_summary = False
    cfg.review.code_context = False
    return cfg


def llm():
    model = MagicMock()
    model.count_tokens.side_effect = lambda text: (len(text) + 3) // 4
    model.review = AsyncMock(return_value=json.dumps({"comments": [], "summary": "checked"}))
    model.usage = {}
    return model


@pytest.mark.parametrize(
    "statement",
    [
        'import { Button } from "../components/Button";',
        'export { Button } from "../components/Button";',
        'const Button = require("../components/Button");',
        'const Button = import("../components/Button");',
        'import "../components/Button";',
        'import { Button } from "../components/Button.js";',
        'import { Button } from "@/components/Button";',
        'import { Button } from "~/components/Button";',
    ],
)
async def test_screen_and_component_are_owned_together(statement):
    files = [
        file("apps/mobile/screens/Home.tsx", "+" + statement),
        file("apps/mobile/components/Button.tsx"),
    ]
    edges = await discover_dependencies(files)
    chunks = chunk_files(files, 10000, dependencies=edges, max_files=1)
    assert len(chunks) == 1
    assert {f.path for f in chunks[0].files} == {f.path for f in files}


async def test_unchanged_import_is_read_from_full_source():
    files = [file("src/page.tsx", "+const value = 2;"), file("src/button.tsx")]
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(
        side_effect=lambda path: (
            'import Button from "./button";' if path.endswith("page.tsx") else ""
        )
    )
    edges = await discover_dependencies(files, fetcher)
    assert edges["src/page.tsx"] == {"src/button.tsx"}
    assert fetcher.fetch.await_count == 2


async def test_transitive_dependencies_and_cycles():
    files = [
        file("src/a.ts", '+import { b } from "./b"'),
        file("src/b.ts", '+export { c } from "./c"'),
        file("src/c.ts", '+import { a } from "./a"'),
        file("unrelated.ts"),
    ]
    edges = await discover_dependencies(files)
    chunks = chunk_files(files, 10000, dependencies=edges, max_files=1)
    assert {f.path for f in chunks[0].files} == {"src/a.ts", "src/b.ts", "src/c.ts"}
    assert [f.path for f in chunks[1].files] == ["unrelated.ts"]


async def test_nearest_app_alias_does_not_join_another_app():
    files = [
        file("apps/mobile/screens/Home.tsx", '+import Button from "@/components/Button"'),
        file("apps/mobile/components/Button.tsx"),
        file("apps/web/components/Button.tsx"),
    ]
    edges = await discover_dependencies(files)
    assert edges[files[0].path] == {"apps/mobile/components/Button.tsx"}


async def test_unchanged_barrel_connects_changed_consumer_and_component():
    files = [
        file("src/page.tsx", '+import { Button } from "./components"'),
        file("src/components/Button.tsx"),
    ]
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(
        side_effect=lambda path: (
            'export { Button } from "./Button"' if path.endswith("index.ts") else ""
        )
    )
    edges = await discover_dependencies(files, fetcher, repo_tree={"src/components/index.ts"})
    assert edges["src/page.tsx"] == {"src/components/Button.tsx"}


async def test_python_relative_and_from_package_imports():
    files = [
        file("src/pkg/page.py", "+from . import button\n+from pkg import service", "python"),
        file("src/pkg/button.py", language="python"),
        file("src/pkg/service.py", language="python"),
    ]
    edges = await discover_dependencies(files)
    assert edges[files[0].path] == {"src/pkg/button.py", "src/pkg/service.py"}


async def test_source_failure_falls_back_to_diff_and_index_edges():
    files = [
        file("src/page.tsx", '+import Button from "./button"'),
        file("src/button.tsx"),
        file("src/types.ts"),
    ]
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(side_effect=RuntimeError("unavailable"))
    edges = await discover_dependencies(files, fetcher, {"src/page.tsx": ["src/types.ts"]})
    assert edges["src/page.tsx"] == {"src/button.tsx", "src/types.ts"}


async def test_rename_keeps_old_import_dependency():
    button = file("src/new.tsx")
    button.old_path = "src/old.tsx"
    edges = await discover_dependencies(
        [file("src/page.tsx", '-import Button from "./old"'), button]
    )
    assert edges["src/page.tsx"] == {"src/new.tsx"}


def test_split_preserves_all_hunks_and_coordinates():
    source = "".join(f'+line_{i} = "' + "x" * 100 + '"\n' for i in range(200))
    big = file("big.py", "@@ -0,0 +1,200 @@\n" + source, "python")
    big.hunks[0] = HunkInfo(0, 0, 1, 200, big.hunks[0].content)
    big.hunks.append(HunkInfo(500, 1, 700, 1, "@@ -500 +700 @@\n-last\n+TAIL_MARKER\n"))
    chunks = chunk_files([big], 4000)
    hunks = [h for c in chunks for f in c.files for h in f.hunks]
    assert len(chunks) > 1
    assert all(c.token_estimate <= 2000 for c in chunks)
    assert sum(h.target_length for h in hunks) == 201
    assert [h.target_start for h in hunks] == sorted(h.target_start for h in hunks)
    rebuilt = "".join("".join(h.content.splitlines(keepends=True)[1:]) for h in hunks)
    assert rebuilt == source + "-last\n+TAIL_MARKER\n"
    assert len(big.hunks) == 2  # Input is not mutated.


def test_oversized_group_keeps_agent_identity_across_parts():
    files = [
        file("a.ts", "+" + "a" * 7000),
        file("b.ts", "+" + "b" * 7000),
        file("c.ts", "+independent"),
    ]
    chunks = chunk_files(files, 4000, dependencies={"a.ts": {"b.ts"}})
    related = [c for c in chunks if "a.ts" in c.related_paths]
    assert len(related) >= 2
    assert len({c.group_id for c in related}) == 1
    assert all(c.related_paths == ["a.ts", "b.ts"] for c in related)
    assert all("c.ts" not in [f.path for f in c.files] for c in related)


async def test_52_files_complete_in_automatic_waves_with_bounded_concurrency():
    config = engine_config()
    config.filter.max_files = 2
    config.review.max_chunks_per_review = 3
    config.review.max_concurrent_chunks = 2
    config.review.max_diff_size = 20
    config.review.max_file_size = 10
    model = llm()
    active = peak = 0

    async def review(messages):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return '{"comments": [], "summary": "checked"}'

    model.review.side_effect = review
    paths = [f"src/file_{i}.py" for i in range(52)]
    result = await ReviewEngine(config=config, llm=model).review_diff(diff(paths))
    assert result.reviewed_files == 52
    assert set(result.reviewed_paths) == set(paths)
    assert result.skipped_paths == []
    assert model.review.await_count == 26
    assert peak == 2


async def test_failed_part_retries_without_repeating_successful_parts(monkeypatch):
    import mira.core.engine as module

    monkeypatch.setattr(
        module, "chunk_files", lambda files, **kw: [ReviewChunk([f]) for f in files]
    )
    model = llm()
    calls = {"src/a.py": 0, "src/b.py": 0}

    async def review(messages):
        path = "src/a.py" if "### `src/a.py`" in messages[-1]["content"] else "src/b.py"
        calls[path] += 1
        if path == "src/b.py" and calls[path] == 1:
            raise RuntimeError("transient")
        return '{"comments": []}'

    model.review.side_effect = review
    result = await ReviewEngine(config=engine_config(), llm=model).review_diff(diff(list(calls)))
    assert calls == {"src/a.py": 1, "src/b.py": 2}
    assert result.reviewed_files == 2 and not result.skipped_paths
    assert any(e["stage"] == "chunk_retry" for e in result.audit)


async def test_partial_file_is_never_reported_fully_reviewed(monkeypatch):
    import mira.core.engine as module

    def parts(files, **kw):
        return [
            ReviewChunk([files[0]], group_id=0),
            ReviewChunk([files[0]], group_id=0),
            ReviewChunk([files[1]], group_id=1),
        ]

    monkeypatch.setattr(module, "chunk_files", parts)
    model = llm()
    a_calls = 0

    async def review(messages):
        nonlocal a_calls
        if "### `src/a.py`" in messages[-1]["content"]:
            a_calls += 1
            if a_calls > 1:
                raise RuntimeError("persistent")
        return '{"comments": []}'

    model.review.side_effect = review
    result = await ReviewEngine(config=engine_config(), llm=model).review_diff(
        diff(["src/a.py", "src/b.py"])
    )
    assert result.reviewed_paths == ["src/b.py"]
    assert result.skipped_paths == ["src/a.py"]
    assert result.reviewed_files == 1


async def test_same_group_runs_sequentially_with_previous_notes(monkeypatch):
    import mira.core.engine as module

    monkeypatch.setattr(
        module,
        "chunk_files",
        lambda files, **kw: [
            ReviewChunk([f], group_id=0, related_paths=[x.path for x in files]) for f in files
        ],
    )
    model = llm()
    model.review.side_effect = [
        '{"comments": [], "summary": "contract requires nullable input"}',
        '{"comments": []}',
    ]
    result = await ReviewEngine(config=engine_config(), llm=model).review_diff(
        diff(["a.py", "b.py"])
    )
    second = model.review.call_args_list[1].args[0][-1]["content"]
    assert "contract requires nullable input" in second
    assert "a.py" in second and "b.py" in second
    assert result.reviewed_files == 2


async def test_all_parts_failing_raises():
    model = llm()
    model.review.side_effect = RuntimeError("provider offline")
    with pytest.raises(RuntimeError, match="provider offline"):
        await ReviewEngine(config=engine_config(), llm=model).review_diff(diff(["a.py"]))
    assert model.review.await_count == 2


async def test_oversized_file_reaches_tail_in_engine():
    config = engine_config()
    config.review.agent_token_budget = 4000
    model = llm()
    paths = ["src/large.py"]
    result = await ReviewEngine(config=config, llm=model).review_diff(diff(paths, lines=4000))
    assert result.reviewed_paths == paths and result.skipped_paths == []
    assert model.review.await_count > 1
    prompts = [call.args[0][-1]["content"] for call in model.review.call_args_list]
    assert any("+value_3999 = 3999" in prompt for prompt in prompts)
    assert all(
        sum(model.count_tokens(m["content"]) for m in call.args[0]) + config.llm.max_tokens
        <= config.llm.max_context_tokens
        for call in model.review.call_args_list
    )


async def test_configured_exclusions_still_apply():
    config = engine_config()
    config.filter.exclude_patterns = ["ignored/**"]
    result = await ReviewEngine(config=config, llm=llm()).review_diff(
        diff(["src/a.py", "ignored/a.py"])
    )
    assert result.reviewed_paths == ["src/a.py"]
    assert result.total_paths == ["src/a.py"]


async def test_review_rest_scope_is_respected():
    engine = ReviewEngine(config=engine_config(), llm=llm())
    engine._review_only_paths = {"src/b.py"}
    result = await engine.review_diff(diff(["src/a.py", "src/b.py"]))
    assert result.reviewed_paths == ["src/b.py"]
    assert result.skipped_paths == []


async def test_dependency_source_and_tree_use_reviewed_sha_without_code_context():
    from mira.models import PRInfo

    provider = MagicMock()
    provider.get_file_content = AsyncMock(return_value='import Button from "./button"')
    provider.get_repo_tree = AsyncMock(return_value=["src/page.tsx", "src/button.tsx"])
    provider.get_file_history = AsyncMock(return_value={})
    model = llm()
    config = engine_config()
    config.review.agent_max_files = 1
    scope = PRInfo("title", "", "main", "feature", "url", 1, "test", "repo", head_sha="review-sha")
    engine = ReviewEngine(config=config, llm=model, provider=provider)
    result = await engine.review_diff(diff(["src/page.tsx", "src/button.tsx"]), repo_scope=scope)
    assert result.reviewed_files == 2
    assert model.review.await_count == 1
    assert all(call.args[2] == "review-sha" for call in provider.get_file_content.call_args_list)
    assert provider.get_repo_tree.call_args.args[1] == "review-sha"


async def test_cancellation_is_not_retried():
    model = llm()
    model.review.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await ReviewEngine(config=engine_config(), llm=model).review_diff(diff(["a.py"]))
    assert model.review.await_count == 1
