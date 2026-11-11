"""Pydantic request/response models and OpenAPI examples."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Result management
# ---------------------------------------------------------------------------


class ResultCreateRequest(BaseModel):
    result_id: str = Field(..., min_length=1, max_length=128, examples=["R-001"])
    sample_code: str = Field(..., min_length=1, max_length=128, examples=["W-2026-0001"])
    item: str = Field(..., min_length=1, max_length=128, examples=["pH"])
    value: str = Field(..., min_length=1, max_length=128, examples=["7.2"])
    reason: str = Field(
        default="initial",
        max_length=512,
        description="修订原因；初次登记通常为 initial。",
        examples=["initial"],
    )


class Revision(BaseModel):
    revision: int
    value: str
    reason: str
    created_at: str


class ResultResponse(BaseModel):
    result_id: str
    sample_code: str
    item: str
    current_revision: int
    created_at: str
    updated_at: str
    revisions: list[Revision]


class ResultCorrectRequest(BaseModel):
    new_value: str = Field(..., min_length=1, max_length=128, examples=["7.4"])
    reason: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="更正原因（必填，便于审计与撤回记录）。",
        examples=["仪器复核后更正"],
    )


# ---------------------------------------------------------------------------
# Preview / publish
# ---------------------------------------------------------------------------


class PreviewRequest(BaseModel):
    sample_code: str = Field(..., min_length=1, max_length=128, examples=["W-2026-0001"])
    result_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=30,
        description="同一水样下 1～30 个结果 ID。",
        examples=[["R-001", "R-002", "R-003"]],
    )

    @field_validator("result_ids")
    @classmethod
    def _unique_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("result_ids 不得包含重复项")
        if any(not rid.strip() for rid in value):
            raise ValueError("result_id 不得为空字符串")
        return value


class PreviewItem(BaseModel):
    index: int
    result_id: str
    sample_code: str
    item: str
    value: str
    current_revision: int
    revision_created_at: str
    all_revisions: list[Revision]


class PreviewResponse(BaseModel):
    sample_code: str
    item_count: int
    items: list[PreviewItem]


class PublishSelection(BaseModel):
    result_id: str = Field(..., examples=["R-001"])
    revision: int = Field(
        ...,
        ge=1,
        description="预览时取得的修订号；发布时必须仍是当前修订。",
        examples=[1],
    )


class PublishRequest(BaseModel):
    sample_code: str = Field(..., min_length=1, max_length=128, examples=["W-2026-0001"])
    selections: list[PublishSelection] = Field(
        ...,
        min_length=1,
        max_length=30,
        description="1～30 条携带修订号的选择。",
    )

    @field_validator("selections")
    @classmethod
    def _unique_selections(cls, value: list[PublishSelection]) -> list[PublishSelection]:
        ids = [selection.result_id for selection in value]
        if len(set(ids)) != len(ids):
            raise ValueError("selections 中 result_id 不得重复")
        return value


# ---------------------------------------------------------------------------
# Stored report representation
# ---------------------------------------------------------------------------


class ReportItem(BaseModel):
    index: int
    result_id: str
    sample_code: str
    item: str
    value: str
    revision: int
    revision_created_at: str


class ReportResponse(BaseModel):
    report_id: str
    sample_code: str
    status: Literal["valid", "withdrawn"]
    etag: str
    created_at: str
    published_at: str
    withdrawn_at: str | None = None
    withdraw_reason: str | None = None
    item_count: int
    items: list[ReportItem]


class PublishResponse(BaseModel):
    report_id: str
    status: Literal["valid"]
    etag: str
    sample_code: str
    item_count: int
    items: list[ReportItem]
    published_at: str
