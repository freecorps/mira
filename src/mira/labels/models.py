"""Validated graph shared by the visual editor, preview and webhook runner."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

FieldName = Literal[
    "total_lines",
    "additions",
    "deletions",
    "changed_files",
    "author",
    "title",
    "description",
    "base_branch",
    "head_branch",
    "files",
    "draft",
]
Operator = Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "starts_with", "glob"]
NUMERIC_FIELDS = {"total_lines", "additions", "deletions", "changed_files"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Condition(StrictModel):
    field: FieldName = "total_lines"
    operator: Operator = "lte"
    value: str | int | bool = 500

    @model_validator(mode="after")
    def valid_comparison(self) -> Self:
        if self.field in NUMERIC_FIELDS:
            if type(self.value) is not int or self.value < 0:
                raise ValueError("Line/file comparisons require a non-negative integer")
            if self.operator not in {"eq", "ne", "gt", "gte", "lt", "lte"}:
                raise ValueError("Invalid numeric operator")
        elif self.field == "draft":
            if type(self.value) is not bool or self.operator not in {"eq", "ne"}:
                raise ValueError("Draft comparisons require a boolean and eq/ne")
        else:
            if not isinstance(self.value, str) or not self.value or len(self.value) > 500:
                raise ValueError("Text comparisons require 1–500 characters")
            if self.operator not in {"eq", "ne", "contains", "starts_with", "glob"}:
                raise ValueError("Invalid text operator")
        return self


class LabelAction(StrictModel):
    name: str = Field(min_length=1, max_length=50)
    color: str = Field(default="0e8a16", pattern=r"^[0-9a-fA-F]{6}$")
    description: str = Field(default="", max_length=100)
    mode: Literal["sync", "add"] = "sync"

    @model_validator(mode="after")
    def valid_name(self) -> Self:
        if self.name != self.name.strip() or any(ord(c) < 32 or c == "," for c in self.name):
            raise ValueError(
                "Label names cannot have outer whitespace, commas or control characters"
            )
        return self


class Position(StrictModel):
    x: float = Field(default=0, allow_inf_nan=False)
    y: float = Field(default=0, allow_inf_nan=False)


class WorkflowNode(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    kind: Literal["start", "condition", "label"]
    position: Position = Field(default_factory=Position)
    condition: Condition | None = None
    action: LabelAction | None = None

    @model_validator(mode="after")
    def valid_payload(self) -> Self:
        if (self.kind == "condition") != (self.condition is not None):
            raise ValueError("Only condition nodes must have a condition")
        if (self.kind == "label") != (self.action is not None):
            raise ValueError("Only label nodes must have an action")
        return self


class WorkflowEdge(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    source: str
    target: str
    branch: Literal["next", "true", "false"] = "next"


class LabelWorkflow(StrictModel):
    version: Literal[1] = 1
    enabled: bool = False
    nodes: list[WorkflowNode] = Field(default_factory=list, max_length=100)
    edges: list[WorkflowEdge] = Field(default_factory=list, max_length=300)

    @model_validator(mode="after")
    def valid_graph(self) -> Self:
        if not self.nodes and not self.edges and not self.enabled:
            return self
        by_id = {node.id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError("Node IDs must be unique")
        starts = [n.id for n in self.nodes if n.kind == "start"]
        if len(starts) != 1:
            raise ValueError("A workflow must have exactly one PR event node")
        if len({e.id for e in self.edges}) != len(self.edges):
            raise ValueError("Edge IDs must be unique")
        outgoing: dict[str, list[str]] = {key: [] for key in by_id}
        incoming = dict.fromkeys(by_id, 0)
        connections: set[tuple[str, str, str]] = set()
        for edge in self.edges:
            if edge.source not in by_id or edge.target not in by_id:
                raise ValueError("Connections must reference existing nodes")
            source_node, target_node = by_id[edge.source], by_id[edge.target]
            if target_node.kind == "start" or source_node.kind == "label":
                raise ValueError("PR event nodes are roots and label nodes are endpoints")
            if (source_node.kind == "condition") != (edge.branch in {"true", "false"}):
                raise ValueError("Conditions require a true/false output; events require next")
            connection_key = (edge.source, edge.target, edge.branch)
            if connection_key in connections:
                raise ValueError("Duplicate connection")
            connections.add(connection_key)
            outgoing[edge.source].append(edge.target)
            incoming[edge.target] += 1
        ready = [key for key, degree in incoming.items() if degree == 0]
        visited = 0
        while ready:
            key = ready.pop()
            visited += 1
            for target in outgoing[key]:
                incoming[target] -= 1
                if incoming[target] == 0:
                    ready.append(target)
        if visited != len(by_id):
            raise ValueError("Workflow cycles are not allowed")
        reachable: set[str] = set()
        pending = list(starts)
        while pending:
            key = pending.pop()
            if key not in reachable:
                reachable.add(key)
                pending.extend(outgoing[key])
        if reachable != set(by_id):
            raise ValueError("Connect every node to the PR event before saving")
        actions: dict[str, LabelAction] = {}
        for node in self.nodes:
            if node.action:
                key = node.action.name.casefold()
                if key in actions and actions[key] != node.action:
                    raise ValueError("Actions for the same label must have identical settings")
                actions[key] = node.action
        if self.enabled and not actions:
            raise ValueError("An enabled workflow requires at least one label action")
        return self


class PRFacts(StrictModel):
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    changed_files: int = Field(default=0, ge=0)
    author: str = Field(default="", max_length=500)
    title: str = Field(default="", max_length=10000)
    description: str = Field(default="", max_length=1000000)
    base_branch: str = Field(default="", max_length=1000)
    head_branch: str = Field(default="", max_length=1000)
    files: list[str] = Field(default_factory=list, max_length=100000)
    draft: bool = False

    @property
    def total_lines(self) -> int:
        return self.additions + self.deletions


class Evaluation(StrictModel):
    matched_nodes: list[str]
    labels: list[LabelAction]
    managed_labels: list[str]
    total_lines: int


class PreviewRequest(StrictModel):
    workflow: LabelWorkflow
    facts: PRFacts
    current_labels: list[str] = Field(default_factory=list, max_length=1000)


class PreviewResponse(StrictModel):
    evaluation: Evaluation
    add: list[str]
    remove: list[str]
