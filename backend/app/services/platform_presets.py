from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class PlatformError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Placement:
    id: str
    name: str
    ratio: str
    width: int
    height: int
    safe_area: dict[str, float]

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ratio": self.ratio,
            "width": self.width,
            "height": self.height,
            "safe_area": dict(self.safe_area),
        }


@dataclass(frozen=True)
class Platform:
    id: str
    name: str
    summary: str
    contact_policy: str
    default_placement_id: str
    placements: tuple[Placement, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "summary": self.summary,
            "contact_policy": self.contact_policy,
            "default_placement_id": self.default_placement_id,
            "placements": [item.public_dict() for item in self.placements],
        }


GENERAL_SAFE_AREA = {"top": 0.05, "right": 0.05, "bottom": 0.05, "left": 0.05}
STORY_SAFE_AREA = {"top": 0.15, "right": 0.06, "bottom": 0.20, "left": 0.06}

PLATFORMS: tuple[Platform, ...] = (
    Platform(
        "xiaohongshu",
        "小红书",
        "图文笔记与封面",
        "forbidden",
        "note",
        (
            Placement("note", "图文笔记 / 封面", "3:4", 1080, 1440, GENERAL_SAFE_AREA),
        ),
    ),
    Platform(
        "instagram",
        "Instagram",
        "帖子、Story 与 Reels 共用一套品牌视觉",
        "required",
        "portrait",
        (
            Placement("portrait", "竖版帖子", "4:5", 1080, 1350, GENERAL_SAFE_AREA),
            Placement("square", "方形帖子", "1:1", 1080, 1080, GENERAL_SAFE_AREA),
            Placement("story", "Story / Reels 封面", "9:16", 1080, 1920, STORY_SAFE_AREA),
        ),
    ),
    Platform(
        "tiktok",
        "TikTok",
        "视频封面；为平台界面预留安全区",
        "required",
        "cover",
        (
            Placement("cover", "视频封面", "9:16", 1080, 1920, STORY_SAFE_AREA),
        ),
    ),
    Platform(
        "google-business-profile",
        "Google Business Profile",
        "商家动态与门店展示",
        "required",
        "update",
        (
            Placement("update", "商家动态", "4:3", 1440, 1080, GENERAL_SAFE_AREA),
        ),
    ),
    Platform(
        "meta-ads",
        "Meta 广告",
        "Feed、Story 与横版广告位",
        "required",
        "feed",
        (
            Placement("feed", "Feed", "4:5", 1080, 1350, GENERAL_SAFE_AREA),
            Placement("story", "Story / Reels", "9:16", 1080, 1920, STORY_SAFE_AREA),
            Placement("landscape", "横版广告", "16:9", 1920, 1080, GENERAL_SAFE_AREA),
        ),
    ),
)

PLATFORM_BY_ID = {item.id: item for item in PLATFORMS}

def catalog() -> list[dict[str, Any]]:
    return [item.public_dict() for item in PLATFORMS]


def get_platform(platform_id: str | None) -> Platform:
    key = str(platform_id or "").strip().lower()
    platform = PLATFORM_BY_ID.get(key)
    if platform is None:
        raise PlatformError("未知的发布平台")
    return platform


def resolve_placement(platform_id: str | None, placement_id: str | None) -> tuple[Platform, Placement]:
    platform = get_platform(platform_id)
    requested = str(placement_id or platform.default_placement_id).strip().lower()
    placement = next((item for item in platform.placements if item.id == requested), None)
    if placement is None:
        raise PlatformError("发布用途与所选平台不匹配")
    return platform, placement


def _clean_merchant_info(value: dict[str, Any] | None) -> dict[str, str] | None:
    if not value:
        return None
    address = str(value.get("address") or "").strip()
    phone = str(value.get("phone") or "").strip()
    if not address and not phone:
        return None
    return {"address": address, "phone": phone}


def build_task_config(
    platform_id: str | None,
    placement_id: str | None,
    merchant_info: dict[str, Any] | None = None,
    *,
    ratio: str | None = None,
) -> dict[str, Any]:
    platform, placement = resolve_placement(platform_id, placement_id)
    if ratio and ratio != placement.ratio:
        raise PlatformError("显式比例与平台预设不一致")
    merchant = _clean_merchant_info(merchant_info)
    if platform.contact_policy == "required":
        if not merchant or not merchant.get("address") or not merchant.get("phone"):
            raise PlatformError("该平台必须填写商家地址和联系电话")
    else:
        merchant = None
    return {
        "platform": platform.public_dict(),
        "placement": placement.public_dict(),
        "ratio": placement.ratio,
        "canvas": {"width": placement.width, "height": placement.height},
        "merchant_info": merchant,
    }


def prompt_context(
    platform: Platform,
    placement: Placement,
    merchant_info: dict[str, Any] | None,
) -> str:
    safe = placement.safe_area
    lines = [
        "平台发布规则（必须遵守）：",
        f"- 发布平台：{platform.name}；用途：{placement.name}；成图尺寸 {placement.width}×{placement.height}。",
        f"- 关键文字、Logo 和联系方式需位于安全区内：上 {safe['top']:.0%}、右 {safe['right']:.0%}、下 {safe['bottom']:.0%}、左 {safe['left']:.0%}。背景可铺满画布。",
    ]
    if platform.contact_policy == "forbidden":
        lines.append("- 小红书图片中不得出现地址、电话、手机号、微信号、二维码或其他联系方式；不要编造联系信息。")
    else:
        merchant = merchant_info or {}
        lines.extend(
            [
                "- 必须清晰展示以下真实商家信息，不得改写、拆分或编造：",
                f"  - 地址：{merchant.get('address', '')}",
                f"  - 电话：{merchant.get('phone', '')}",
            ]
        )
    return "\n".join(lines)


def prompt_from_task(
    platform_snapshot: dict[str, Any] | None,
    merchant_info: dict[str, Any] | None,
    placement_snapshot: dict[str, Any] | None = None,
) -> str:
    if not isinstance(platform_snapshot, dict) or not platform_snapshot.get("id"):
        return ""
    placement_id = (
        str(placement_snapshot.get("id") or "")
        if isinstance(placement_snapshot, dict)
        else str(platform_snapshot.get("default_placement_id") or "")
    )
    platform, placement = resolve_placement(platform_snapshot.get("id"), placement_id)
    return prompt_context(platform, placement, merchant_info)


def ensure_merchant_copy_items(
    copy_items: list[dict[str, Any]],
    platform_snapshot: dict[str, Any] | None,
    merchant_info: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(platform_snapshot, dict) or platform_snapshot.get("contact_policy") != "required":
        return copy_items
    merchant = merchant_info or {}
    items = list(copy_items)
    existing = "\n".join(str(item.get("text") or "") for item in items if isinstance(item, dict))
    for key, role, label, copy_id in (
        ("address", "details", "地址", "copy_merchant_address"),
        ("phone", "details", "电话", "copy_merchant_phone"),
    ):
        value = str(merchant.get(key) or "").strip()
        text = f"{label}：{value}" if value else ""
        if text and text not in existing and value not in existing:
            items.append({"id": copy_id, "role": role, "text": text, "source": "user"})
    return items
