from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class TaskCreate(BaseModel):
    product_asset_ids: list[str] = Field(min_length=1, max_length=4)
    logo_asset_id: str | None = None
    brand_id: str | None = Field(default=None, max_length=64)
    prompt: str = Field(default="", max_length=2000)
    ratio: Literal["1:1", "9:16", "4:5", "16:9"] = "1:1"

    @field_validator("brand_id", mode="before")
    @classmethod
    def normalize_brand_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        return text or None

    @field_validator("prompt", mode="before")
    @classmethod
    def normalize_prompt(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()


class EditRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=1000)

    @field_validator("instruction", mode="before")
    @classmethod
    def normalize_instruction(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()


class LayerPatch(BaseModel):
    content: str | None = Field(default=None, max_length=500)
    style: dict[str, Any] | None = None
    position: dict[str, float] | None = None
    size: dict[str, float] | None = None
    visible: bool | None = None
    opacity: float | None = Field(default=None, ge=0.05, le=1.0)
    rotation: float | None = Field(default=None, ge=-180, le=180)
    asset_id: str | None = None
    mode: Literal["model_integrated", "overlay"] | None = None

    @model_validator(mode="after")
    def require_something(self) -> "LayerPatch":
        if not self.model_fields_set:
            raise ValueError("至少需要修改一个字段")
        return self

    def to_patch(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True, exclude_unset=True)


class AssetOut(BaseModel):
    id: str
    kind: str
    original_name: str
    width: int
    height: int
    mime_type: str
    url: str
    created_at: datetime


class TaskOut(BaseModel):
    id: str
    status: str
    stage: str
    progress: int = 0
    input: dict[str, Any]
    canvas: dict[str, int]
    provider: dict[str, Any]
    background_asset_id: str | None = None
    output_asset_id: str | None = None
    preview_url: str | None = None
    download_url: str | None = None
    error: dict[str, str] | None = None
    created_at: datetime
    updated_at: datetime
