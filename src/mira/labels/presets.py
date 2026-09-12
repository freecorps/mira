"""Editable starter graphs; presets use the exact same schema as custom flows."""

from mira.labels.models import LabelWorkflow


def size_preset() -> LabelWorkflow:
    nodes: list[dict] = [{"id": "start", "kind": "start", "position": {"x": 40, "y": 200}}]
    edges: list[dict] = []
    previous = "start"
    for i, (name, limit, color) in enumerate(
        [
            ("XS", 10, "c2e0c6"),
            ("S", 100, "7bd88f"),
            ("M", 500, "f9d65c"),
            ("L", 1000, "f5a461"),
            ("XL", None, "e05d5d"),
        ]
    ):
        label_id = f"size-{name}"
        x = 340 + i * 310
        if limit is not None:
            condition_id = f"limit-{name}"
            nodes.append(
                {
                    "id": condition_id,
                    "kind": "condition",
                    "position": {"x": x, "y": 200},
                    "condition": {"field": "total_lines", "operator": "lte", "value": limit},
                }
            )
            edges.append(
                {
                    "id": f"to-{condition_id}",
                    "source": previous,
                    "target": condition_id,
                    "branch": "next" if i == 0 else "false",
                }
            )
            edges.append(
                {
                    "id": f"to-{label_id}",
                    "source": condition_id,
                    "target": label_id,
                    "branch": "true",
                }
            )
            previous = condition_id
        else:
            edges.append(
                {"id": f"to-{label_id}", "source": previous, "target": label_id, "branch": "false"}
            )
        nodes.append(
            {
                "id": label_id,
                "kind": "label",
                "position": {"x": x, "y": 420},
                "action": {"name": f"size/{name}", "color": color, "mode": "sync"},
            }
        )
    return LabelWorkflow.model_validate({"nodes": nodes, "edges": edges})


def simple_preset(field: str, operator: str, value: str, label: str) -> LabelWorkflow:
    return LabelWorkflow.model_validate(
        {
            "nodes": [
                {"id": "start", "kind": "start", "position": {"x": 40, "y": 100}},
                {
                    "id": "condition",
                    "kind": "condition",
                    "position": {"x": 350, "y": 100},
                    "condition": {"field": field, "operator": operator, "value": value},
                },
                {
                    "id": "label",
                    "kind": "label",
                    "position": {"x": 680, "y": 100},
                    "action": {"name": label, "color": "1d76db", "mode": "sync"},
                },
            ],
            "edges": [
                {"id": "event-condition", "source": "start", "target": "condition"},
                {
                    "id": "condition-label",
                    "source": "condition",
                    "target": "label",
                    "branch": "true",
                },
            ],
        }
    )


def presets() -> list[dict]:
    return [
        {
            "id": "size",
            "name": "PR size",
            "description": "XS ≤10, S ≤100, M ≤500, L ≤1,000, XL >1,000. Additions + deletions.",
            "workflow": size_preset().model_dump(),
        },
        {
            "id": "author",
            "name": "PR author",
            "description": "Label PRs from a specific author. Replace the example login.",
            "workflow": simple_preset("author", "eq", "octocat", "author/octocat").model_dump(),
        },
        {
            "id": "paths",
            "name": "Documentation",
            "description": "Label PRs that touch docs/**. Change the path pattern to match your project.",
            "workflow": simple_preset("files", "glob", "docs/**", "documentation").model_dump(),
        },
    ]
