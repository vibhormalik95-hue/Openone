"""The two public tool contracts; schemas are generated from these models."""

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

UUIDField = Annotated[UUID, Field(strict=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Source(StrictModel):
    client: str = Field(min_length=1, max_length=80)
    conversation_id: str = Field(min_length=1, max_length=200)
    message_id: str | None = Field(default=None, max_length=200)


class MemoryItem(StrictModel):
    kind: Literal["decision", "task", "state", "note"]
    text: str = Field(min_length=1, max_length=4000)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        # Preserve exact source text for evidence and idempotency. No lossy NLP deduplication.
        if not value.strip():
            raise ValueError("text must contain non-whitespace characters")
        return value

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if len(json.dumps(value, ensure_ascii=False).encode()) > 2048:
            raise ValueError("metadata exceeds 2048 bytes")
        return value


class ConstraintChange(StrictModel):
    key: str = Field(min_length=1, max_length=96, pattern=r"^[a-z0-9_.-]+$")
    value: JsonValue
    expected_version: int = Field(
        ge=0,
        le=2_147_483_646,
        description="Use recall.constraint_versions[key] when present, including superseded keys; otherwise 0.",
    )
    status: Literal["active", "superseded"] = "active"

    @field_validator("value")
    @classmethod
    def bounded_value(cls, value: JsonValue) -> JsonValue:
        if len(json.dumps(value, ensure_ascii=False).encode()) > 2048:
            raise ValueError("constraint value exceeds 2048 bytes")
        return value


class CommitMemoryRequest(StrictModel):
    project_id: UUIDField
    idempotency_key: UUIDField
    source: Source
    memories: list[MemoryItem] = Field(default_factory=list, max_length=20)
    constraints: list[ConstraintChange] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_batch(self) -> "CommitMemoryRequest":
        if not self.memories and not self.constraints:
            raise ValueError("at least one memory or constraint is required")
        keys = [change.key for change in self.constraints]
        if len(keys) != len(set(keys)):
            raise ValueError("a constraint key may appear only once in a batch")
        if len(self.model_dump_json().encode()) > 120_000:
            raise ValueError("commit payload exceeds 120000 bytes")
        return self

    def database_payload(self) -> dict:
        return self.model_dump(mode="json", exclude={"project_id", "idempotency_key"})


class RecallMemoryRequest(StrictModel):
    project_id: UUIDField
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=8, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def meaningful_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must contain non-whitespace characters")
        return value
