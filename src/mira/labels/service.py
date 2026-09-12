"""Reconcile only workflow-owned labels, always against freshly fetched PR data."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from weakref import WeakValueDictionary

from mira.labels.engine import evaluate, plan
from mira.labels.models import PRFacts
from mira.labels.store import load_workflow, scope_key
from mira.providers.base import BaseProvider

logger = logging.getLogger(__name__)
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


async def reconcile(
    provider: BaseProvider, db: Any, platform: str, owner: str, repo: str, number: int, url: str
) -> dict:
    key = scope_key(platform, owner, repo) + f":{number}"
    # Queue newer deliveries, never drop them because an older event is running.
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        workflow = load_workflow(db, platform, owner, repo)
        if not workflow.enabled:
            return {"status": "disabled"}
        for _ in range(3):
            pr = await provider.get_pr_info(url)
            if (pr.platform, pr.owner, pr.repo, pr.number) != (platform, owner, repo, number):
                raise ValueError("PR identity does not match the workflow repository")
            fields = {node.condition.field for node in workflow.nodes if node.condition}
            stats = (
                await provider.get_label_change_stats(pr)
                if fields & {"total_lines", "additions", "deletions", "changed_files", "files"}
                else []
            )
            facts = PRFacts(
                additions=sum(file.added_lines for file in stats),
                deletions=sum(file.deleted_lines for file in stats),
                changed_files=len(stats),
                files=[file.path for file in stats],
                author=pr.author,
                title=pr.title,
                description=pr.description,
                base_branch=pr.base_branch,
                head_branch=pr.head_branch,
                draft=pr.draft,
            )
            current = await provider.get_pr_labels(pr)
            latest = await provider.get_pr_info(url)
            if latest == pr:
                break
        else:
            raise RuntimeError("PR kept changing during label evaluation; try the next event")
        # Do not publish a workflow that was edited/disabled while fetching the PR.
        if load_workflow(db, platform, owner, repo) != workflow:
            return {"status": "configuration_changed"}
        state_key = "label_state:" + key
        previous = json.loads(db.get_setting(state_key) or "[]")
        evaluation = evaluate(workflow, facts)
        # All 'add only' actions relinquish synchronization, even when unmatched.
        additive = {
            node.action.name.casefold()
            for node in workflow.nodes
            if node.action and node.action.mode == "add"
        }
        previous = [name for name in previous if name.casefold() not in additive]
        result = plan(evaluation, current, previous)
        # Retain ownership until each removal succeeds, including deleted/renamed rules.
        managed = set(previous) | set(evaluation.managed_labels)
        db.set_setting(state_key, json.dumps(sorted(managed)))
        actions = {label.name: label for label in evaluation.labels}
        # Prepare every missing definition before changing the PR itself.
        for name in result.add:
            if load_workflow(db, platform, owner, repo) != workflow:
                return {"status": "configuration_changed"}
            label = actions[name]
            await provider.ensure_label(pr, label.name, label.color, label.description)
        # Remove obsolete size labels first so no stable result has two size tiers.
        for name in result.remove:
            if load_workflow(db, platform, owner, repo) != workflow:
                return {"status": "configuration_changed"}
            await provider.remove_label(pr, name)
            # A retired rule stops owning its label as soon as removal succeeds,
            # even if a later write fails. Current sync rules still own their
            # configured labels on subsequent evaluations by design.
            managed = {owned for owned in managed if owned.casefold() != name.casefold()}
            db.set_setting(state_key, json.dumps(sorted(managed)))
        for name in result.add:
            if load_workflow(db, platform, owner, repo) != workflow:
                return {"status": "configuration_changed"}
            await provider.add_label(pr, name)
        if load_workflow(db, platform, owner, repo) != workflow:
            return {"status": "configuration_changed"}
        db.set_setting(state_key, json.dumps(evaluation.managed_labels))
        logger.info(
            "PR labels synchronized for %s: added=%s removed=%s", url, result.add, result.remove
        )
        return {"status": "ok", **result.model_dump()}
