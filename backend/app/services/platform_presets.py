from __future__ import annotations

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
    default_placement_id: str
    placements: tuple[Placement, ...]
    content_rules: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "summary": self.summary,
            "default_placement_id": self.default_placement_id,
            "placements": [item.public_dict() for item in self.placements],
            "content_rules": list(self.content_rules),
        }


GENERAL_SAFE_AREA = {"top": 0.05, "right": 0.05, "bottom": 0.05, "left": 0.05}
STORY_SAFE_AREA = {"top": 0.15, "right": 0.06, "bottom": 0.20, "left": 0.06}
FREEFORM_PLATFORM_CONTEXT = (
    "发布平台未指定：请根据主题、素材、品牌规范和用户文案，"
    "自主选择最合适的构图、信息层级与视觉方向；不要套用某个平台的专属规则，"
    "也不要编造平台名称。"
)

PLATFORMS: tuple[Platform, ...] = (
    Platform(
        "xiaohongshu",
        "小红书",
        "图文笔记与封面",
        "note",
        (
            Placement("note", "图文笔记 / 封面", "3:4", 1080, 1440, GENERAL_SAFE_AREA),
        ),
        content_rules=(
            "禁止出现地址、电话号码或二维码（包括文字、图标、贴纸或可扫码图案）；"
            "用户文案如包含相关内容，必须省略。",
        ),
    ),
    Platform(
        "instagram",
        "Instagram",
        "帖子、Story 与 Reels 共用一套品牌视觉",
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
        "cover",
        (
            Placement("cover", "视频封面", "9:16", 1080, 1920, STORY_SAFE_AREA),
        ),
    ),
    Platform(
        "google-business-profile",
        "Google Business Profile",
        "商家动态与门店展示",
        "update",
        (
            Placement("update", "商家动态", "4:3", 1440, 1080, GENERAL_SAFE_AREA),
        ),
    ),
    Platform(
        "meta-ads",
        "Meta 广告",
        "Feed、Story 与横版广告位",
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


def build_task_config(
    platform_id: str | None,
    placement_id: str | None,
    *,
    ratio: str | None = None,
) -> dict[str, Any]:
    platform, placement = resolve_placement(platform_id, placement_id)
    if ratio and ratio != placement.ratio:
        raise PlatformError("显式比例与平台预设不一致")
    return {
        "platform": platform.public_dict(),
        "placement": placement.public_dict(),
        "ratio": placement.ratio,
        "canvas": {"width": placement.width, "height": placement.height},
    }


def prompt_context(
    platform: Platform,
    placement: Placement,
) -> str:
    safe = placement.safe_area
    lines = [
        "平台发布规则（必须遵守）：",
        f"- 发布平台：{platform.name}；用途：{placement.name}；成图尺寸 {placement.width}×{placement.height}。",
        f"- 关键文字和 Logo 需位于安全区内：上 {safe['top']:.0%}、右 {safe['right']:.0%}、下 {safe['bottom']:.0%}、左 {safe['left']:.0%}。背景可铺满画布。",
    ]
    if platform.content_rules:
        lines.append("内容限制（必须遵守）：")
        lines.extend(f"- {rule}" for rule in platform.content_rules)
    return "\n".join(lines)


def freeform_context() -> str:
    return FREEFORM_PLATFORM_CONTEXT


def prompt_from_task(
    platform_snapshot: dict[str, Any] | None,
    placement_snapshot: dict[str, Any] | None = None,
) -> str:
    if not isinstance(platform_snapshot, dict) or not platform_snapshot.get("id"):
        return freeform_context()
    placement_id = (
        str(placement_snapshot.get("id") or "")
        if isinstance(placement_snapshot, dict)
        else str(platform_snapshot.get("default_placement_id") or "")
    )
    platform, placement = resolve_placement(platform_snapshot.get("id"), placement_id)
    return prompt_context(platform, placement)
