"""One Core AI run per new series submission, with images assigned by the Agent."""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.schemas import PosterSeriesRunCreate, WholeImageTaskCreate, gateway_model_name
from app.services import poster_costs, poster_series as series, whole_image
from app.services.canvas import image_model_supports_canvas
from app.services.core_ai import AgentArtifact


logger = logging.getLogger(__name__)
WORKFLOW = "agent-series"
AGENT_LOGO_DELIVERY = "agent_composited"
AZURE_BLOB_SAS_FIELDS = {"se", "sig", "sip", "sp", "spr", "sr", "st", "sv"}


def create_run(
    db: Any, settings: Any, workspace_id: str, payload: PosterSeriesRunCreate, *,
    build_task: Callable[[WholeImageTaskCreate], dict[str, Any]], launch: Callable[[str], None],
    merchant_id: str = "",
) -> dict[str, Any]:
    series.require_enabled(settings)
    workspace_id = workspace_id.strip()
    if not workspace_id:
        raise series.SeriesError("invalid_workspace", "工作区 ID 不能为空", 422)
    key = {"workspace_id": workspace_id, "submit_id": payload.submit_id}
    if db.whole_series_runs.find_one(key):
        return series.get_workspace_series(db, workspace_id, settings, merchant_id=merchant_id)
    references = list(payload.reference_asset_ids or payload.product_asset_ids or [])
    models = series._group_pages_payload(payload)
    template = build_task(series._series_task_payload(
        payload, workspace_id=workspace_id, model=models[0]["image_model"], page_prompt=payload.prompt,
        page_assets=[{"asset_id": asset_id} for asset_id in references], title=payload.workspace_title,
    ))
    template_input = template["input"]
    canvas = template["canvas"]
    for spec in models:
        model = spec["image_model"]
        if not image_model_supports_canvas(model, canvas["width"], canvas["height"]):
            raise series.SeriesError("unsupported_format", f"图片模型 {model} 不支持 {template_input['ratio']} 画幅", 422)
    references = list(template_input.get("reference_asset_ids") or references)
    style_references = list(template_input.get("style_reference_asset_ids") or payload.style_reference_asset_ids)
    logo_id = template_input.get("logo_asset_id") or payload.logo_asset_id
    references = [asset_id for asset_id in references if asset_id != logo_id]
    logo_position = payload.logo_position if logo_id else None
    logo_hint = series.resolve_logo_box(canvas, payload.prompt, position_override=logo_position) if logo_id else None
    if logo_hint and logo_hint["source"] != "user":
        logo_hint = None

    existed = db.whole_workspace_modes.find_one({"workspace_id": workspace_id}) is not None
    series.claim_workspace_mode(db, workspace_id, "series")
    run_id = f"wsrun_{uuid.uuid4().hex}"
    now = whole_image.utcnow()
    run = {"_id": run_id, **key, "workflow": WORKFLOW, "status": "preparing",
           "round": db.whole_series_runs.count_documents({"workspace_id": workspace_id}) + 1,
           "prompt": payload.prompt, "title": payload.workspace_title, "canvas": canvas, "logo_position": logo_position,
           "created_at": now, "updated_at": now}
    try:
        db.whole_series_runs.insert_one(run)
    except DuplicateKeyError:
        return series.get_workspace_series(db, workspace_id, settings, merchant_id=merchant_id)
    try:
        for spec in models:
            db.whole_series_groups.insert_one({
                "_id": f"wsgrp_{uuid.uuid4().hex}", "workspace_id": workspace_id, "run_id": run_id,
                "order": spec["order"], "image_model": spec["image_model"], "page_count": 0,
                "status": "queued", "prompt": payload.prompt, "canvas": canvas,
                "logo_asset_id": logo_id, "logo_box": logo_hint, "logo_box_source": "user" if logo_hint else "agent",
                "content_pool": references, "style_reference_ids": style_references, "agent_asset_urls": {},
                "task_template": series._task_template(template), "created_at": now, "updated_at": now,
            })
        db.whole_series_runs.update_one({"_id": run_id}, {"$set": {"status": "queued"}})
    except Exception:
        db.whole_series_groups.delete_many({"run_id": run_id})
        db.whole_series_runs.delete_one({"_id": run_id})
        if not existed and not db.whole_series_runs.find_one({"workspace_id": workspace_id}):
            db.whole_workspace_modes.delete_one({"workspace_id": workspace_id, "mode": "series"})
        raise
    launch(run_id)
    return series.get_workspace_series(db, workspace_id, settings, merchant_id=merchant_id)


def _agent_input(db: Any, settings: Any, store: Any, client: Any,
                 run: dict[str, Any], groups: list[dict[str, Any]]) -> dict[str, Any]:
    group = groups[0]
    context = group["task_template"]
    brand = context.get("brand") or {}
    style_ids = list(dict.fromkeys([*group["style_reference_ids"], *(brand.get("reference_asset_ids") or [])]))
    logo_id = group.get("logo_asset_id")
    ids = [asset_id for asset_id in dict.fromkeys([*group["content_pool"], *style_ids]) if asset_id != logo_id]
    urls = series._agent_asset_urls(db, settings, store, client, group, ids)
    input_ids = [*ids, logo_id] if logo_id else ids
    rows = {str(item["_id"]): item for item in db.assets.find({"_id": {"$in": input_ids}})}
    references = [{"asset_id": asset_id, "url": urls[asset_id],
                   "name": (rows.get(asset_id) or {}).get("original_name"),
                   "role": "content" if asset_id in group["content_pool"] else "style"}
                  for asset_id in ids]
    logo = None
    if logo_id:
        references.append({
            "asset_id": logo_id,
            "url": series._upload_asset_url(db, settings, store, client, logo_id),
            "name": (rows.get(logo_id) or {}).get("original_name") or "官方 Logo",
            "role": "logo",
        })
        hint = group.get("logo_box") or {}
        placement = {key: hint[key] for key in ("position", "size") if hint.get(f"{key}_specified")}
        artwork = series._logo_artwork(db, store, logo_id, run["canvas"])
        logo_position = run.get("logo_position")
        instruction = (
            "For each group choose one supplied option before drawing any page. "
            "Use its exact logo_box for all pages and keep only its safe_area clear. "
            "Use the poster-series skill to overlay the official reference and visually review the composited PNG. "
            "Register the reviewed PNG and its no-Logo base separately."
        )
        if logo_position:
            instruction += " The selected Logo position takes precedence over position wording in the design prompt."
        logo = {
            "artwork": artwork,
            "delivery": AGENT_LOGO_DELIVERY,
            "placement": placement or None,
            "options": series._logo_options(run["canvas"], run["prompt"], artwork, position_override=logo_position),
            "instruction": instruction,
        }
    return {
        "operation": "poster-series-generate", "prompt": run["prompt"], "canvas": run["canvas"],
        "groups": [{"id": item["_id"], "model": item["image_model"]} for item in groups],
        "references": references,
        "logo": logo,
        "brand": {key: brand[key] for key in ("name", "palette", "layout_guide", "background_guide") if brand.get(key)} or None,
        "store_info": context.get("merchant_context") or None,
    }


def _artifact_url_key(url: str) -> str:
    # Authentication parameters can differ between links to the same current Azure blob.
    try:
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.netloc.endswith(".blob.core.windows.net"):
            query = parse_qs(parsed.query, keep_blank_values=True)
            if query.get("sr") == ["b"] and query.get("sig") and query.keys() <= AZURE_BLOB_SAS_FIELDS:
                return parsed._replace(query="").geturl()
    except ValueError:
        pass
    return url


def _read_manifest(
    result: Any, groups: list[dict[str, Any]], *, logo_artwork: dict[str, Any] | None = None,
    logo_options: list[dict[str, Any]] | None = None,
    style_reference_ids: frozenset[str] = frozenset(),
    artifact_redirect: Callable[[str], str | None] | None = None,
    agent_composited_logo: bool = False,
) -> dict[str, dict[str, Any]]:
    manifest = series._extract_agent_payload(result.output)
    if manifest.get("error"):
        raise series.SeriesError("agent_generation_failed", str(manifest["error"]), 502)
    delivered = manifest.get("groups")
    if not isinstance(delivered, list):
        raise series.SeriesError("agent_contract_invalid", "Agent 没有返回有效的整组 JSON 清单", 502)
    if len(delivered) != len(groups):
        raise series.SeriesError("agent_contract_invalid", "Agent 返回的组数与请求不一致", 502)
    requested = {group["_id"]: group for group in groups}
    artifacts_by_url = {
        item.download_url: item for item in result.artifacts
        if item.content_type.split(";")[0].strip().lower() == "image/png"
    }
    urls_by_name: dict[str, set[str]] = {}
    for url, artifact in artifacts_by_url.items():
        urls_by_name.setdefault(artifact.file_name, set()).add(url)
    urls_by_redirect: dict[str, set[str]] | None = None
    indexed: dict[str, dict[str, Any]] = {}
    used: set[str] = set()

    def take_png(url: Any, *, file_name: Any = None, missing: str) -> AgentArtifact:
        nonlocal urls_by_redirect
        if url is None:
            matches = urls_by_name.get(str(file_name or ""), set())
            url = next(iter(matches)) if len(matches) == 1 else None
        elif isinstance(url, str) and url not in artifacts_by_url and artifact_redirect is not None:
            if urls_by_redirect is None:
                urls_by_redirect = {}
                for registered_url in artifacts_by_url:
                    if redirect := artifact_redirect(registered_url):
                        key = _artifact_url_key(redirect)
                        urls_by_redirect.setdefault(key, set()).add(registered_url)
            matches = urls_by_redirect.get(_artifact_url_key(url), set())
            url = next(iter(matches)) if len(matches) == 1 else None
        if not isinstance(url, str) or url not in artifacts_by_url or url in used:
            raise series.SeriesError("agent_image_missing", missing, 502)
        used.add(url)
        return artifacts_by_url[url]

    for item in delivered:
        group = requested.get(str(item.get("id"))) if isinstance(item, dict) else None
        if group is None or group["_id"] in indexed:
            raise series.SeriesError("agent_contract_invalid", "Agent 返回了未知或重复的组", 502)
        if gateway_model_name(item.get("model")) != gateway_model_name(group["image_model"]):
            raise series.SeriesError("agent_model_mismatch", "Agent 返回的图片模型与请求不一致", 502)
        images = item.get("images")
        if not isinstance(images, list) or not images:
            raise series.SeriesError("agent_image_missing", "Agent 没有为这一组交付图片", 502)
        logo_box = None
        if group.get("logo_asset_id"):
            logo_box = series._validate_agent_logo_box(item.get("logo_box"), group["canvas"])
            if logo_options:
                selected = next(
                    (option for option in logo_options
                     if all(logo_box[key] == value for key, value in option["logo_box"].items())),
                    None,
                )
                if selected is None:
                    raise series.SeriesError("agent_logo_invalid", "Agent 使用了未提供的 Logo 安全区", 502)
                logo_box.update(position=selected["position"])
                if group.get("logo_box_source") == "user":
                    logo_box["source"] = "user"
            else:
                logo_box = series.merge_logo_box(
                    group["canvas"], group["prompt"], logo_box, apply_size_hint=logo_artwork is None,
                )
            if logo_artwork:
                logo_box["min_visible_px"] = logo_artwork["min_visible_px"]
        photo_ids = set(group["content_pool"])
        known_ids = photo_ids | style_reference_ids
        if group.get("logo_asset_id"):
            known_ids.add(group["logo_asset_id"])
        outputs = []
        for image in images:
            if not isinstance(image, dict):
                raise series.SeriesError("agent_image_missing", "Agent 交付的图片清单无效", 502)
            artifact = take_png(image.get("image_url"), file_name=image.get("file_name"),
                                missing="Agent 交付的图片文件缺失、重复或无法唯一匹配")
            base_artifact = None
            if agent_composited_logo and group.get("logo_asset_id"):
                base_artifact = take_png(image.get("base_image_url"),
                                         missing="Agent 必须另行提交已注册的无 Logo 底图")
            refs = image.get("content_refs") or []
            if not isinstance(refs, list) or any(
                not isinstance(ref, dict) or not isinstance(ref.get("asset_id"), str)
                or ref["asset_id"] not in known_ids for ref in refs
            ):
                raise series.SeriesError("agent_reference_invalid", "Agent 引用了素材池以外的原照", 502)
            outputs.append({"artifact": artifact, "base_artifact": base_artifact, "content_refs": [
                {"asset_id": ref["asset_id"], "target": str(ref.get("target") or "")}
                for ref in refs if ref["asset_id"] in photo_ids
            ]})
        indexed[group["_id"]] = {"logo_box": logo_box, "images": outputs}
    return indexed


def _save_image(db: Any, settings: Any, store: Any, client: Any, run: dict[str, Any],
                group: dict[str, Any], order: int, item: dict[str, Any]) -> None:
    key = uuid.uuid5(uuid.NAMESPACE_URL, f"{group['_id']}/{order}").hex
    task_id, page_id = f"wtask_{key}", f"wspage_{key}"
    existing = db.whole_tasks.find_one({"_id": task_id})
    if existing and existing.get("status") == "succeeded":
        db.whole_tasks.update_one({"_id": task_id}, {"$set": {"series.status": "succeeded"}})
        db.whole_series_pages.update_one({"_id": page_id}, {"$set": {
            "status": "succeeded", "version_id": existing["current_version_id"],
            "output_id": existing["current_output_id"],
        }})
        return
    # A restart may find a v1 inserted before the task's selected version was published.
    for draft in db.whole_versions.find({"task_id": task_id, "version": 1}):
        for output in whole_image._version_outputs(draft):
            whole_image._delete_asset(db, store, output.get("asset_id"))
        db.whole_versions.delete_one({"_id": draft["_id"]})

    refs = item["content_refs"]
    page = {"_id": page_id, "group_id": group["_id"], "workspace_id": group["workspace_id"],
            "run_id": run["_id"], "order": order, "role": "image", "whole_task_id": task_id,
            "status": "queued", "content_refs": refs, "logo_box": group.get("logo_box"),
            "style_reference_ids": group.get("style_reference_ids") or [],
            "created_at": whole_image.utcnow(), "updated_at": whole_image.utcnow()}
    task = series._new_page_task_doc(db, group, page, role="image", prompt=run["prompt"], task_id=task_id)
    task["series"].update({"agent_run_id": run["agent_run_id"], "status": "publishing"})
    if run.get("title"):
        task["workspace_initial_title"] = run["title"]
    artifact = item["artifact"]
    base_artifact = item.get("base_artifact")
    canvas = group["canvas"]
    width, height = canvas["width"], canvas["height"]
    data = client.download_image(artifact.download_url)
    saved_assets: list[str] = []
    try:
        if base_artifact:
            base_data = client.download_image(base_artifact.download_url)
            if base_data == data:
                raise series.SeriesError("agent_logo_missing", "Agent 交付的成品与无 Logo 底图相同", 502)
            series.validate_png(data, canvas)
            series.validate_png(base_data, canvas)
            saved = whole_image._persist_output_png(db, settings, store, task_id=task_id, version=1,
                                                   png=base_data, role="series_base", size=(width, height))
        else:
            saved = whole_image._save_output_asset(db, settings, store, task_id=task_id, version=1,
                                                   data=data, role="series_base", target_size=(width, height))
        saved_assets.append(saved["asset_id"])
        recorded_refs = run.get("agent_references")
        images = ([{**ref, "role": "style_reference" if ref["role"] == "style" else ref["role"]}
                   for ref in recorded_refs] if recorded_refs is not None else
                  [{"asset_id": ref["asset_id"], "name": ref["target"] or "原照", "role": "content"} for ref in refs])
        model_input = {"provider": "core-ai", "channel": "agent", "endpoint": "agent:poster-series-generate",
                       "model": group["image_model"], "prompt": run["prompt"],
                       "parameters": {"run_id": run["agent_run_id"], "file_name": artifact.file_name,
                                      "reference_scope": "round" if recorded_refs is not None else "image"},
                       "images": images}
        base_output = {"id": "series_base", "channel": "agent", "role": "series_base", "label": "成品",
                       "asset_id": saved["asset_id"], "width": width, "height": height, "model_input": model_input,
                       "created_at": whole_image.utcnow()}
        if base_artifact:
            final = whole_image._persist_output_png(db, settings, store, task_id=task_id, version=1,
                                                   png=data, role="series_final", size=(width, height))
            final_id = final["asset_id"]
            saved_assets.append(final_id)
            outputs = [
                {**base_output, "label": "无 Logo 底图", "selectable": False},
                {**base_output, "id": "series_final", "role": "series_final", "asset_id": final_id,
                 "selectable": True},
            ]
            extra = {"series_base_asset_id": saved["asset_id"], "series_logo_box": group["logo_box"],
                     "series_logo_asset_id": group["logo_asset_id"]}
        else:
            final_id, outputs, extra = series.finalize_series_outputs(db, settings, store, task=task, version=1,
                                                                      outputs=[base_output], canvas=canvas)
            if final_id != saved["asset_id"]:
                saved_assets.append(final_id)
        db.whole_series_pages.replace_one({"_id": page_id}, page, upsert=True)
        db.whole_tasks.replace_one({"_id": task_id}, task, upsert=True)
        version = whole_image._version_document(task_id=task_id, version=1, parent_version_id=None,
                                                output_asset_id=final_id, instruction=run["prompt"],
                                                operation="series_generate", width=width, height=height,
                                                model_input=model_input, outputs=outputs)
        version.update(extra, series_content_refs=refs, series_role="image", series_page_id=page_id)
        whole_image._commit_version(db, store, task_id=task_id, version_doc=version, generation=True)
        db.whole_tasks.update_one({"_id": task_id}, {"$set": {"series.status": "succeeded"}})
        series._update_page(db, page_id, {"status": "succeeded", "version_id": version["_id"],
                                         "output_id": next(output["id"] for output in outputs if output["selectable"])})
    except Exception:
        if not db.whole_tasks.find_one({"_id": task_id, "status": "succeeded"}):
            db.whole_versions.delete_many({"task_id": task_id})
            db.whole_tasks.delete_one({"_id": task_id})
            db.whole_series_pages.delete_one({"_id": page_id})
            for asset_id in saved_assets:
                whole_image._delete_asset(db, store, asset_id)
        raise


def run_generation(db: Any, settings: Any, store: Any, client: Any, run_id: str) -> None:
    run = db.whole_series_runs.find_one_and_update(
        {"_id": run_id, "workflow": WORKFLOW, "status": "queued"},
        {"$set": {"status": "running", "updated_at": whole_image.utcnow()}}, return_document=ReturnDocument.AFTER,
    )
    if run is None:
        return
    groups = list(db.whole_series_groups.find({"run_id": run_id}).sort("order", 1))
    db.whole_series_groups.update_many({"run_id": run_id, "status": "queued"}, {"$set": {"status": "running"}})
    try:
        agent_id = settings.core_ai_series_agent_id
        if not agent_id:
            raise series.SeriesError("series_agent_not_configured", "系列图 Agent 未配置", 503)
        if not run.get("agent_run_id"):
            payload = _agent_input(db, settings, store, client, run, groups)
            with poster_costs.track(db, run["workspace_id"], "series"):
                run["agent_run_id"] = client.trigger_agent(agent_id, json.dumps(payload, ensure_ascii=False), timeout_seconds=30)
            snapshot = {"agent_run_id": run["agent_run_id"], "agent_references": payload["references"],
                        "agent_logo_delivery": (payload["logo"] or {}).get("delivery"),
                        "agent_logo_artwork": (payload["logo"] or {}).get("artwork"),
                        "agent_logo_options": (payload["logo"] or {}).get("options")}
            run.update(snapshot)
            db.whole_series_runs.update_one({"_id": run_id}, {"$set": snapshot})
        result = client.wait_agent_detail(run["agent_run_id"], timeout_seconds=settings.core_ai_series_agent_timeout_seconds)
        style_reference_ids = frozenset(
            ref["asset_id"] for ref in run.get("agent_references", []) if ref["role"] == "style"
        )
        indexed = _read_manifest(result, groups, logo_artwork=run.get("agent_logo_artwork"),
                                 logo_options=run.get("agent_logo_options"),
                                 style_reference_ids=style_reference_ids,
                                 artifact_redirect=client.agent_artifact_redirect,
                                 agent_composited_logo=run.get("agent_logo_delivery") == AGENT_LOGO_DELIVERY)
        for group in groups:
            delivered = indexed[group["_id"]]
            group.update(logo_box=delivered["logo_box"], page_count=len(delivered["images"]))
            db.whole_series_groups.update_one({"_id": group["_id"]}, {"$set": {
                "logo_box": group["logo_box"], "page_count": group["page_count"],
            }})
            for order, image in enumerate(delivered["images"]):
                _save_image(db, settings, store, client, run, group, order, image)
            series._refresh_group(db, group["_id"])
        db.whole_series_runs.update_one({"_id": run_id}, {"$set": {"status": "complete", "updated_at": whole_image.utcnow()}})
    except Exception as exc:
        logger.exception("Agent series generation failed for %s", run_id)
        error = whole_image._error_text(exc)
        db.whole_series_runs.update_one({"_id": run_id}, {"$set": {"status": "failed", "error": error, "updated_at": whole_image.utcnow()}})
        db.whole_series_groups.update_many({"run_id": run_id, "status": {"$ne": "complete"}},
                                           {"$set": {"status": "failed", "error": error}})


def resume_runs(db: Any, settings: Any, store: Any, client: Any) -> None:
    active = {"workflow": WORKFLOW, "status": {"$in": ["queued", "running"]}}
    for run in db.whole_series_runs.find(active):
        if run["status"] == "running":
            if not run.get("agent_run_id"):
                error = "服务重启时未取得 Agent 运行编号，请重新生成"
                db.whole_series_runs.update_one({"_id": run["_id"]}, {"$set": {"status": "failed", "error": error}})
                db.whole_series_groups.update_many({"run_id": run["_id"]}, {"$set": {"status": "failed", "error": error}})
                continue
            claim = db.whole_series_runs.update_one(
                {"_id": run["_id"], "status": "running", "updated_at": run["updated_at"]},
                {"$set": {"status": "queued", "updated_at": whole_image.utcnow()}},
            )
            if not claim.modified_count:
                continue
        run_generation(db, settings, store, client, run["_id"])
