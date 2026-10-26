import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ImageModel = Literal[
    "gpt-image-2.5-sunburst",
    "gemini-3.1-flash-image",
    "seedream-5-pro-image-to-image",
]


class BrandColor(BaseModel):
    hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator("hex", mode="before")
    @classmethod
    def normalize_hex(cls, value: Any) -> str:
        text = str(value or "").strip().upper()
        if not re.fullmatch(r"#[0-9A-F]{6}", text):
            raise ValueError("颜色必须是六位 HEX")
        return text

class DuplicateBrandRequest(BaseModel):
    name: str = Field(default="", max_length=80)
    platform_id: str = Field(default="", max_length=64)
    placement_id: str = Field(default="", max_length=64)

    @field_validator("name", "platform_id", "placement_id", mode="before")
    @classmethod
    def strip_duplicate_text(cls, value: Any) -> str:
        return str(value or "").strip()


class StyleFields(BaseModel):
    palette_notes: str = Field(default="", max_length=4000)
    typography: str = Field(min_length=1, max_length=4000)
    subject_style: str = Field(min_length=1, max_length=4000)
    background: str = Field(min_length=1, max_length=4000)
    layout: str = Field(default="重点清晰、层级明确，正文不遮挡关键主体", max_length=4000)
    decoration: str = Field(default="", max_length=4000)
    identity: str = Field(default="", max_length=4000)
    restrictions: str = Field(default="", max_length=6000)

    @field_validator("*", mode="before")
    @classmethod
    def strip_style_text(cls, value: Any) -> str:
        return str(value or "").strip()


class BrandRulesUpdate(BaseModel):
    version: int = Field(ge=1)
    copy_accuracy: str = Field(min_length=1, max_length=4000)
    asset_usage: str = Field(min_length=1, max_length=4000)
    logo_usage: str = Field(min_length=1, max_length=4000)
    readability: str = Field(min_length=1, max_length=4000)

    @field_validator("copy_accuracy", "asset_usage", "logo_usage", "readability", mode="before")
    @classmethod
    def strip_rules_text(cls, value: Any) -> str:
        return str(value or "").strip()


class BrandUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    short_name: str = Field(default="", max_length=40)
    summary: str = Field(default="", max_length=500)
    platform_id: str = Field(default="", max_length=64)
    placement_id: str = Field(default="", max_length=64)
    colors: list[BrandColor] = Field(default_factory=list, max_length=6)
    logo_asset_ids: list[str] = Field(default_factory=list, max_length=5)
    # Legacy fields remain accepted while existing records and clients migrate.
    platform: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=500)
    layout_guide: str = Field(default="", max_length=20000)
    background_guide: str = Field(default="", max_length=20000)
    schema_version: Literal[1, 2] = 1
    style_fields: StyleFields | None = None
    logo_keywords: list[str] = Field(default_factory=list, max_length=20)
    is_default: bool = False
    cover_asset_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_guides(self):
        if self.schema_version == 2 or self.style_fields is not None:
            if self.style_fields is None or not self.summary:
                raise ValueError("请填写风格定位和风格字段")
            if not self.colors and not self.style_fields.palette_notes:
                raise ValueError("请填写色值或配色说明")
            self.schema_version = 2
        elif not self.layout_guide or not self.background_guide:
            raise ValueError("品牌排版指南和背景指南不能为空")
        return self

    @field_validator(
        "name",
        "short_name",
        "summary",
        "platform_id",
        "placement_id",
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

    @field_validator("logo_asset_ids")
    @classmethod
    def normalize_logo_asset_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))[:5]


class WholeImageTaskCreate(BaseModel):
    image_model: ImageModel = "gpt-image-2.5-sunburst"
    prompt: str = Field(min_length=1, max_length=5000)
    prompt_optimized: bool = False
    ratio: Literal["1:1", "9:16", "4:5", "16:9", "3:4", "4:3"] = "9:16"
    product_asset_ids: list[str] = Field(default_factory=list, max_length=6)
    brand_id: str | None = Field(default=None, max_length=64)
    brand_mode: Literal["preset", "reference", "none"] | None = None
    logo_asset_id: str | None = Field(default=None, max_length=128)
    style_reference_asset_ids: list[str] = Field(default_factory=list, max_length=3)
    candidate_count: int = Field(default=3, ge=1, le=6)
    candidate_batch_id: str | None = Field(default=None, max_length=128)
    candidate_index: int = Field(default=0, ge=0, le=5)
    platform_id: str | None = Field(default=None, max_length=64)
    placement_id: str | None = Field(default=None, max_length=64)

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

    @field_validator("logo_asset_id", mode="before")
    @classmethod
    def normalize_logo_asset_id(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None


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
    logo_asset_id: str | None = Field(default=None, max_length=128)
    style_reference_asset_ids: list[str] = Field(default_factory=list, max_length=3)
    product_asset_ids: list[str] = Field(default_factory=list, max_length=6)
    platform_id: str | None = Field(default=None, max_length=64)
    placement_id: str | None = Field(default=None, max_length=64)

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

    @field_validator("logo_asset_id", mode="before")
    @classmethod
    def normalize_optimizer_logo_id(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("style_reference_asset_ids", "product_asset_ids")
    @classmethod
    def normalize_optimizer_asset_ids(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]
