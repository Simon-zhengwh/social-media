from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class MerchantInfo(BaseModel):
    address: str = Field(default="", max_length=240)
    phone: str = Field(default="", max_length=80)

    @field_validator("address", "phone", mode="before")
    @classmethod
    def normalize_contact(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()


class TaskCreate(BaseModel):
    product_asset_ids: list[str] = Field(default_factory=list, max_length=4)
    brand_id: str | None = Field(default=None, max_length=64)
    brand_mode: Literal["preset", "reference", "none"] | None = None
    style_reference_asset_ids: list[str] = Field(default_factory=list, max_length=3)
    candidate_count: int = Field(default=1, ge=1, le=3)
    candidate_batch_id: str | None = Field(default=None, max_length=128)
    candidate_index: int = Field(default=0, ge=0, le=2)
    prompt: str = Field(default="", max_length=2000)
    ratio: Literal["1:1", "9:16", "4:5", "16:9", "3:4", "4:3"] = "1:1"
    generation_mode: Literal["classic", "reference_guided"] = "reference_guided"
    platform_id: str | None = Field(default=None, max_length=64)
    placement_id: str | None = Field(default=None, max_length=64)
    merchant_info: MerchantInfo | None = None

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
    content: str | dict[str, Any] | None = None
    style: dict[str, Any] | None = None
    compositing: dict[str, Any] | None = None
    position: dict[str, float] | None = None
    size: dict[str, float] | None = None
    visible: bool | None = None
    opacity: float | None = Field(default=None, ge=0.05, le=1.0)
    rotation: float | None = Field(default=None, ge=-180, le=180)
    asset_id: str | None = None
    fit: Literal["contain", "cover"] | None = None
    crop: dict[str, float] | None = None
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

class BrandUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    short_name: str = Field(default="", max_length=40)
    summary: str = Field(default="", max_length=500)
    layout_guide: str = Field(min_length=1, max_length=20000)
    background_guide: str = Field(min_length=1, max_length=20000)
    logo_keywords: list[str] = Field(default_factory=list, max_length=20)
    is_default: bool = False
    cover_asset_id: str | None = Field(default=None, max_length=128)

    @field_validator("name", "short_name", "summary", "layout_guide", "background_guide", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("logo_keywords")
    @classmethod
    def normalize_logo_keywords(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            text = str(item).strip().lower()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
        return result


class WholeImageTaskCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    ratio: Literal["1:1", "9:16", "4:5", "16:9", "3:4", "4:3"] = "9:16"
    product_asset_ids: list[str] = Field(default_factory=list, max_length=4)
    brand_id: str | None = Field(default=None, max_length=64)
    brand_mode: Literal["preset", "reference", "none"] | None = None
    style_reference_asset_ids: list[str] = Field(default_factory=list, max_length=3)
    candidate_count: int = Field(default=1, ge=1, le=3)
    candidate_batch_id: str | None = Field(default=None, max_length=128)
    candidate_index: int = Field(default=0, ge=0, le=2)
    platform_id: str | None = Field(default=None, max_length=64)
    placement_id: str | None = Field(default=None, max_length=64)
    merchant_info: MerchantInfo | None = None

    @field_validator("brand_id", mode="before")
    @classmethod
    def normalize_whole_brand_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        return text or None

    @field_validator("prompt", mode="before")
    @classmethod
    def normalize_whole_prompt(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()


class WholeImageEditRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=1000)
    source_version_id: str = Field(min_length=1, max_length=128)

    @field_validator("instruction", "source_version_id", mode="before")
    @classmethod
    def normalize_whole_edit_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()
