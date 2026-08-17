"""Structured output contracts for every model task (section 8.4).

If model output cannot be parsed into these schemas, the task fails and
nothing is written to the database (fail-safe, section 3.5).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

CANDIDATE_TYPES = {
    "idea",
    "opinion",
    "decision",
    "question",
    "principle",
    "hypothesis",
    "insight",
    "risk",
    "method",
}
ACTIONS = {"create_root", "create_child", "append", "revise", "merge"}
RELATION_TYPES = {
    "related_to",
    "supports",
    "contradicts",
    "refines",
    "qualifies",
    "depends_on",
    "applies_to",
    "inspired_by",
    "supersedes",
}


class CaptureResult(BaseModel):
    """Light normalization + candidate proposal for one user utterance."""

    candidate_type: str = "idea"
    normalized_content: str
    title: str = Field(max_length=120)
    proposed_action: str = "create_root"
    proposed_parent_ids: list[str] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    needs_clarification: bool = False

    @field_validator("candidate_type")
    @classmethod
    def _type_ok(cls, v: str) -> str:
        if v not in CANDIDATE_TYPES:
            raise ValueError(f"invalid candidate_type {v}")
        return v

    @field_validator("proposed_action")
    @classmethod
    def _action_ok(cls, v: str) -> str:
        if v not in ACTIONS:
            raise ValueError(f"invalid proposed_action {v}")
        return v


class CaptureBatchResult(BaseModel):
    """One utterance may contain several independent thoughts."""

    items: list[CaptureResult] = Field(min_length=1, max_length=10)


class PlacementResult(BaseModel):
    """Tree placement decision made after the content is normalized."""

    proposed_action: str = "create_root"
    proposed_parent_ids: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("proposed_action")
    @classmethod
    def _action_ok(cls, v: str) -> str:
        if v not in {"create_root", "create_child", "append", "revise"}:
            raise ValueError(f"invalid placement action {v}")
        return v


class DailyExtractItem(BaseModel):
    """One thought extracted from daily chat; excerpt indexes the source."""

    source_message_no: int = Field(ge=1)
    source_excerpt: str
    capture: CaptureResult


class DailyExtractResult(BaseModel):
    items: list[DailyExtractItem] = Field(default_factory=list)


class RelationProposal(BaseModel):
    to_node_id: str
    relation_type: str
    direction: str = "new_to_existing"
    rationale: str = Field(max_length=300)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("relation_type")
    @classmethod
    def _rel_ok(cls, v: str) -> str:
        if v not in RELATION_TYPES:
            raise ValueError(f"invalid relation_type {v}")
        return v

    @field_validator("direction")
    @classmethod
    def _direction_ok(cls, v: str) -> str:
        if v not in {"new_to_existing", "existing_to_new"}:
            raise ValueError(f"invalid relation direction {v}")
        return v


class RelationProposals(BaseModel):
    relations: list[RelationProposal] = Field(default_factory=list, max_length=5)


class DictationTopic(BaseModel):
    """One coherent topic composed from a dictation session."""

    title: str = Field(max_length=120)
    content: str = Field(min_length=1)
    candidate_type: str = "idea"

    @field_validator("candidate_type")
    @classmethod
    def _type_ok(cls, v: str) -> str:
        if v not in CANDIDATE_TYPES:
            raise ValueError(f"invalid candidate_type {v}")
        return v


class DictationMergeResult(BaseModel):
    """Output of the dictation_merge task.

    Default is ONE topic per session; multiple topics only when the owner
    explicitly enumerated independent items. An empty list is legal (e.g. a
    session holding assistant context only); the daily worker falls back to
    the owner's verbatim words whenever those would otherwise be lost.
    """

    topics: list[DictationTopic] = Field(default_factory=list, max_length=5)


class ChildMergeProposal(BaseModel):
    """Reorganize: append source child's content into target child (user-confirmed)."""

    source_node_id: str
    target_node_id: str
    rationale: str = Field(max_length=300)
    confidence: float = Field(ge=0.0, le=1.0)


class ChildRelationProposal(BaseModel):
    """Reorganize: semantic relation between two children of the same parent."""

    from_node_id: str
    to_node_id: str
    relation_type: str
    rationale: str = Field(max_length=300)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("relation_type")
    @classmethod
    def _rel_ok(cls, v: str) -> str:
        if v not in RELATION_TYPES:
            raise ValueError(f"invalid relation_type {v}")
        return v


class ReorganizeResult(BaseModel):
    """Output of the tree-reorganize task: proposals only, never actions."""

    merges: list[ChildMergeProposal] = Field(default_factory=list, max_length=5)
    relations: list[ChildRelationProposal] = Field(default_factory=list, max_length=8)


class QueryRelevance(BaseModel):
    """Precision filter after recall: only nodes that actually answer the question."""

    node_ids: list[str] = Field(default_factory=list, max_length=8)


class QueryCitation(BaseModel):
    node_id: str
    revision_no: int
    path: str


class QueryAnswer(BaseModel):
    answer: str
    citations: list[QueryCitation] = Field(default_factory=list)
    new_inferences: list[str] = Field(
        default_factory=list,
        description="Conclusions formed in THIS answer, not user's stored thoughts",
    )
