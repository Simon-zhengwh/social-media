from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ImageModel = Literal[
    "gpt-image-2.5-sunburst",
    "gemini-3.1-flash-image",
    "seedream-5-pro-image-to-image",
]


class MerchantInfo(BaseModel):
    address: str = Field(default="", max_length=240)
    phone: str = Field(default="", max_length=80)

    @field_validator("address", "phone", mode="before")
    @classmethod
    def normalize_contact(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()


class BrandUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    short_name: str = Field(default="", max_length=40)
    summary: str = Field(default="", max_length=500)
    platform: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=500)
    layout_guide: str = Field(min_length=1, max_length=20000)
    background_guide: str = Field(min_length=1, max_length=20000)
    logo_keywords: list[str] = Field(default_factory=list, max_length=20)
    is_default: bool = False
    cover_asset_id: str | None = Field(default=None, max_length=128)

    @field_validator(
        "name",
        "short_name",
        "summary",
        "platform",
        "note",
        "layout_guide",
        "background_guide",
        mode="before",
    )
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
    image_model: ImageModel = "gpt-image-2.5-sunburst"
    prompt: str = Field(min_length=1, max_length=5000)
    prompt_optimized: bool = False
    ratio: Literal["1:1", "9:16", "4:5", "16:9", "3:4", "4:3"] = "9:16"
    product_asset_ids: list[str] = Field(default_factory=list, max_length=4)
    brand_id: str | None = Field(default=None, max_length=64)
    brand_mode: Literal["preset", "reference", "none"] | None = None
    style_reference_asset_ids: list[str] = Field(default_factory=list, max_length=3)
    candidate_count: int = Field(default=3, ge=1, le=6)
    candidate_batch_id: str | None = Field(default=None, max_length=128)
    candidate_index: int = Field(default=0, ge=0, le=5)
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


class PromptOptimizeRequest(BaseModel):
    prompt: str = Field(default="", max_length=5000)
    brand_mode: Literal["preset", "reference", "none"] = "none"
    brand_id: str | None = Field(default=None, max_length=64)
    style_reference_asset_ids: list[str] = Field(default_factory=list, max_length=3)
    product_asset_ids: list[str] = Field(default_factory=list, max_length=4)
    platform_id: str | None = Field(default=None, max_length=64)
    placement_id: str | None = Field(default=None, max_length=64)
    merchant_info: MerchantInfo | None = None

    @field_validator("prompt", mode="before")
    @classmethod
    def normalize_optimizer_prompt(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("brand_id", mode="before")
    @classmethod
    def normalize_optimizer_brand_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        return text or None

    @field_validator("style_reference_asset_ids", "product_asset_ids")
    @classmethod
    def normalize_optimizer_asset_ids(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]
