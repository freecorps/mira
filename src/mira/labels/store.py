"""Independent settings rows isolate repositories and hosting platforms."""

import json
from typing import Any

from mira.labels.models import LabelWorkflow


def scope_key(platform: str, owner: str, repo: str) -> str:
    return json.dumps([platform, owner, repo], ensure_ascii=True, separators=(",", ":"))


def load_workflow(db: Any, platform: str, owner: str, repo: str) -> LabelWorkflow:
    raw = db.get_setting("label_workflow:" + scope_key(platform, owner, repo))
    return LabelWorkflow.model_validate_json(raw) if raw else LabelWorkflow()


def save_workflow(db: Any, platform: str, owner: str, repo: str, workflow: LabelWorkflow) -> None:
    db.set_setting("label_workflow:" + scope_key(platform, owner, repo), workflow.model_dump_json())
