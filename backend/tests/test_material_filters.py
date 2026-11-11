from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import mongomock
import pytest

from app.config import Settings
from app.services import material_filter_classifier as classifier
from app.services import material_filter_files as mf
from app.services import material_filters as store
from app.services.scoped_db import ScopedDatabase


@pytest.fixture
def db():
    return ScopedDatabase(mongomock.MongoClient().db, "merchant-a")


@pytest.fixture
def settings(tmp_path):
    return Settings(
        storage_dir=str(tmp_path / "storage"),
        am_bypass_local=True,
        social_media_origin="http://127.0.0.1:5173",
        core_ai_api_key="test-key",
    )


def _folder(db, value="\\\\192.168.11.5\\movies\\See Joy素材\\照片"):
    folder, _ = store.add_folder(db, value, readable_check=lambda _: None)
    return folder


def _scheme(db, settings, folder=None, name="排除空座位和蟹腿", **kwargs):
    folder = folder or _folder(db)
    return store.create_scheme(db, settings, folder, name=name, **kwargs)


def _entry(relative="a.jpg", data=b"image-bytes", mtime_ns=123):
    return mf.SourceEntry(
        relative_path=relative,
        name=relative.split("/")[-1],
        size=len(data),
        mtime_ns=mtime_ns,
        absolute_path=f"\\\\source\\{relative}",
    )


def _snapshot(entry, data=b"image-bytes", mtime_ns=123):
    return mf.SourceSnapshot(
        relative_path=entry.relative_path,
        size=len(data),
        mtime_ns=mtime_ns,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def _result(decision="pass"):
    verdict = {"pass": "pass", "fail": "fail", "uncertain": "uncertain"}[decision]
    return classifier.ClassificationResult(
        name="红烧肉砂锅特写",
        category="食物",
        tags=["红烧肉", "砂锅", "浓汁", "热菜"],
        description="一只砂锅装着红烧肉。",
        verdicts=[
            classifier.RuleVerdict("empty_seating_scene", verdict, "无餐桌椅。"),
            classifier.RuleVerdict("crab_legs", "pass", "无蟹腿。"),
        ],
        decision=decision,
        token_usage={},
    )


def _evaluation(db, scheme, *, relative="a.jpg", data=b"image-bytes", decision="pass", mtime_ns=123):
    entry = _entry(relative, data, mtime_ns)
    snapshot = _snapshot(entry, data, mtime_ns)
    file_doc = store.upsert_file(
        db,
        scheme,
        entry,
        size=snapshot.size,
        mtime_ns=snapshot.mtime_ns,
        sha256=snapshot.sha256,
        latest_evaluation_id=None,
    )
    result = None if decision == "error" else _result(decision)
    evaluation = store.save_evaluation(
        db,
        scheme,
        file_doc,
        snapshot,
        result=result,
        decision=decision,
        error="model_request_failed" if decision == "error" else None,
        preview_path=None,
        output_path=None,
        output_name=None,
        copy_status="none",
        copy_error=None,
        short_id="abcd1234",
    )
    store.upsert_file(
        db,
        scheme,
        entry,
        size=snapshot.size,
        mtime_ns=snapshot.mtime_ns,
        sha256=snapshot.sha256,
        latest_evaluation_id=evaluation["_id"],
    )
    return evaluation


def test_add_folder_deduplicates_equivalent_addresses_and_keeps_source_on_rename(db):
    folder, created = store.add_folder(
        db, "\\\\192.168.11.5\\movies\\See Joy素材\\照片", readable_check=lambda _: None
    )
    assert created is True
    assert folder["merchant_id"] == "merchant-a"

    again, created_again = store.add_folder(
        db,
        "smb://192.168.11.5/movies/See%20Joy%E7%B4%A0%E6%9D%90/%E7%85%A7%E7%89%87",
        readable_check=lambda _: None,
    )
    assert created_again is False
    assert again["_id"] == folder["_id"]
    assert len(store.list_folders(db)) == 1

    renamed = store.rename_folder(db, folder["_id"], "See Joy 照片")
    assert renamed["display_name"] == "See Joy 照片"
    assert renamed["source_root"] == folder["source_root"]
    assert renamed["created_at"] == folder["created_at"]


def test_folder_display_names_disambiguate_same_leaf_directory(db):
    first, _ = store.add_folder(db, "\\\\nas-a\\photos\\照片", readable_check=lambda _: None)
    second, _ = store.add_folder(db, "\\\\nas-b\\photos\\照片", readable_check=lambda _: None)
    assert first["display_name"] != second["display_name"]
    assert first["display_name"].startswith("照片")


def test_add_folder_reports_unreadable_source_without_writing(db):
    def deny(_):
        raise mf.MaterialFilterFileError("source_unreadable", "无法读取来源目录", 422)

    with pytest.raises(mf.MaterialFilterFileError):
        store.add_folder(db, "\\\\nas\\share\\照片", readable_check=deny)
    assert store.list_folders(db) == []


def test_create_scheme_snapshots_rules_and_model(db, settings):
    scheme = _scheme(db, settings, recursive=False)
    assert scheme["source_root_snapshot"] == "\\\\192.168.11.5\\movies\\See Joy素材\\照片"
    assert [rule["id"] for rule in scheme["rules"]] == ["empty_seating_scene", "crab_legs"]
    assert scheme["model"] == settings.material_filter_model
    assert scheme["prompt_version"] == classifier.PROMPT_VERSION
    assert scheme["recursive"] is False
    assert scheme["active_run_id"] is None
    assert scheme["output_root"] == str(settings.storage_path / "material-filters")


def test_create_scheme_rejects_network_or_relative_output_roots(db, settings):
    folder = _folder(db)
    with pytest.raises(store.MaterialFilterError) as excinfo:
        store.create_scheme(db, settings, folder, name="x", output_root="\\\\nas\\share")
    assert excinfo.value.code == "output_network_path_rejected"
    with pytest.raises(store.MaterialFilterError) as excinfo:
        store.create_scheme(db, settings, folder, name="x", output_root="relative/out")
    assert excinfo.value.code == "output_not_absolute"


def test_scheme_rules_are_validated_and_immutable_after_clone(db, settings):
    scheme = _scheme(db, settings)
    with pytest.raises(store.MaterialFilterError) as excinfo:
        store.create_scheme(db, settings, _folder(db), name="重复", rules=[
            {"id": "same", "text": "a"}, {"id": "same", "text": "b"}
        ])
    assert excinfo.value.code == "rule_id_duplicate"
    with pytest.raises(store.MaterialFilterError):
        store.create_scheme(db, settings, _folder(db), name="空", rules=[])

    cloned = store.clone_scheme(db, settings, scheme, name="仅排除空座位", rules=[
        {"id": "empty_seating_scene", "text": "不能出现空座位场景"}
    ])
    assert cloned["_id"] != scheme["_id"]
    assert [rule["id"] for rule in cloned["rules"]] == ["empty_seating_scene"]
    assert len(store.get_scheme(db, scheme["_id"])["rules"]) == 2


def test_scheme_belongs_to_its_folder_only(db, settings):
    folder_a = _folder(db)
    folder_b, _ = store.add_folder(db, "\\\\nas-b\\share\\照片", readable_check=lambda _: None)
    scheme = _scheme(db, settings, folder_a, name="A 方案")
    assert [item["_id"] for item in store.list_schemes(db, folder_a["_id"])] == [scheme["_id"]]
    assert store.list_schemes(db, folder_b["_id"]) == []


def test_merchant_isolation_keeps_folders_schemes_and_results_apart(db, settings):
    folder = _folder(db)
    scheme = _scheme(db, settings, folder)
    _evaluation(db, scheme)
    other = ScopedDatabase(db.raw, "merchant-b")
    assert store.list_folders(other) == []
    with pytest.raises(store.MaterialFilterError) as excinfo:
        store.get_scheme(other, scheme["_id"])
    assert excinfo.value.code == "scheme_not_found"
    assert other.material_filter_evaluations.count_documents({}) == 0
    assert db.material_filter_evaluations.count_documents({}) == 1


def test_runs_are_idempotent_per_request_and_scheme_exclusive(db, settings):
    scheme = _scheme(db, settings)
    run, created = store.create_run(db, scheme, request_id="req-1")
    assert created is True
    assert run["status"] == "queued"

    same, created_again = store.create_run(db, scheme, request_id="req-1")
    assert created_again is False
    assert same["_id"] == run["_id"]

    fresh = store.get_scheme(db, scheme["_id"])
    assert fresh["active_run_id"] == run["_id"]

    with pytest.raises(store.MaterialFilterError) as excinfo:
        store.create_run(db, scheme, request_id="req-2")
    assert excinfo.value.code == "scheme_busy"

    store.release_scheme_run(db, scheme["_id"], run["_id"])
    second, created_second = store.create_run(db, store.get_scheme(db, scheme["_id"]), request_id="req-2")
    assert created_second is True
    assert second["_id"] != run["_id"]


def test_interrupted_runs_are_marked_and_released(db, settings):
    scheme = _scheme(db, settings)
    run, _ = store.create_run(db, scheme, request_id="req")
    assert store.mark_scheme_interrupted(db) == [run["_id"]]
    assert store.get_run(db, run["_id"])["status"] == "interrupted"
    assert store.get_scheme(db, scheme["_id"])["active_run_id"] is None


def test_results_paging_filters_and_counts(db, settings):
    scheme = _scheme(db, settings)
    _evaluation(db, scheme, relative="a.jpg", data=b"aaa", decision="pass")
    _evaluation(db, scheme, relative="b.jpg", data=b"bbb", decision="fail")
    _evaluation(db, scheme, relative="sub/c.jpg", data=b"ccc", decision="uncertain")
    _evaluation(db, scheme, relative="d.jpg", data=b"ddd", decision="error")

    page = store.list_results(db, scheme["_id"], status="all")
    assert page["total"] == 4
    assert page["counts"] == {"all": 4, "pass": 1, "fail": 1, "uncertain": 1, "error": 1, "picked": 0}

    uncertain = store.list_results(db, scheme["_id"], status="uncertain")
    assert [item["relative_path"] for item in uncertain["items"]] == ["sub/c.jpg"]

    searched = store.list_results(db, scheme["_id"], search="红烧肉")
    assert searched["total"] == 3  # error rows have no model name
    path_search = store.list_results(db, scheme["_id"], search="sub/")
    assert path_search["total"] == 1

    page_one = store.list_results(db, scheme["_id"], skip=0, limit=2)
    page_two = store.list_results(db, scheme["_id"], skip=2, limit=2)
    assert len(page_one["items"]) == 2 and len(page_two["items"]) == 2
    assert {item["_id"] for item in page_one["items"]}.isdisjoint(
        {item["_id"] for item in page_two["items"]}
    )


def test_metadata_edits_validate_tags_and_do_not_touch_paths(db, settings):
    scheme = _scheme(db, settings)
    evaluation = _evaluation(db, scheme)
    updated = store.update_metadata(
        db,
        evaluation,
        display_name="招牌红烧肉",
        tags=["红烧肉", "招牌菜", "红烧肉"],
    )
    assert updated["display_name"] == "招牌红烧肉"
    assert updated["tags"] == ["红烧肉", "招牌菜"]
    assert updated["relative_path"] == evaluation["relative_path"]

    with pytest.raises(store.MaterialFilterError):
        store.update_metadata(db, evaluation, display_name="  ")
    with pytest.raises(store.MaterialFilterError) as excinfo:
        store.update_metadata(db, evaluation, tags=["超长" * 20])
    assert excinfo.value.code == "tag_too_long"


def test_export_csv_escapes_formulas_and_quotes(db, settings):
    scheme = _scheme(db, settings)
    evaluation = _evaluation(db, scheme)
    store.update_metadata(db, evaluation, display_name="=SUM(A1)", tags=["带,逗号", '带"引号', "换行\n标签"])
    data = store.export_csv(db, scheme)
    text = data.decode("utf-8-sig")
    assert "'=SUM(A1)" in text
    assert '带""引号' in text
    assert "换行\n标签" in text
    assert text.startswith("名称,分类,标签,状态")

def test_manual_pick_is_a_second_stage_independent_of_the_model(db, settings):
    scheme = _scheme(db, settings)
    evaluation = _evaluation(db, scheme, relative="burst/one.jpg")

    picked = store.set_manual_pick(db, evaluation, picked=True)
    assert picked["manual_pick"] is True
    assert picked["effective_decision"] == evaluation["effective_decision"]
    assert picked["human_decision"] == evaluation["human_decision"]

    page = store.list_results(db, scheme["_id"], status="picked")
    assert page["total"] == 1
    assert page["counts"]["picked"] == 1
    assert page["items"][0]["_id"] == evaluation["_id"]

    store.set_manual_pick(db, evaluation, picked=False)
    assert store.list_results(db, scheme["_id"], status="picked")["total"] == 0
    assert store.list_results(db, scheme["_id"], status="picked")["counts"]["picked"] == 0


def test_results_sort_by_name_path_and_reject_unknown_sort(db, settings):
    scheme = _scheme(db, settings)
    _evaluation(db, scheme, relative="b/second.jpg", data=b"second")
    _evaluation(db, scheme, relative="a/first.jpg", data=b"first")

    by_name = [item["original_name"] for item in store.list_results(db, scheme["_id"], sort="name")["items"]]
    assert by_name == ["first.jpg", "second.jpg"]

    by_path = [item["relative_path"] for item in store.list_results(db, scheme["_id"], sort="path")["items"]]
    assert by_path == ["a/first.jpg", "b/second.jpg"]

    with pytest.raises(store.MaterialFilterError) as excinfo:
        store.list_results(db, scheme["_id"], sort="random")
    assert excinfo.value.code == "invalid_sort"


def test_bulk_pick_only_touches_the_given_scheme(db, settings):
    folder = _folder(db)
    first = store.create_scheme(db, settings, folder, name="A")
    second = store.create_scheme(db, settings, folder, name="B")
    first_eval = _evaluation(db, first)
    second_eval = _evaluation(db, second)

    updated = store.bulk_manual_pick(db, first, [first_eval["_id"], second_eval["_id"]], picked=True)
    assert updated == 1
    assert store.get_evaluation(db, first_eval["_id"])["manual_pick"] is True
    assert store.get_evaluation(db, second_eval["_id"])["manual_pick"] is False

def test_delete_folder_removes_records_and_keeps_other_tabs(db, settings, tmp_path):
    folder = _folder(db)
    other = _folder(db, r"\\nas-b\share\照片")
    scheme = _scheme(db, settings, folder)
    evaluation = _evaluation(db, scheme)
    run, _ = store.create_run(db, scheme, request_id="cleanup")
    store.set_manual_pick(db, evaluation, picked=True)
    # A finished run is deletable; an active one is refused (separate test).
    db.material_filter_runs.update_one({"_id": run["_id"]}, {"$set": {"status": "succeeded"}})
    store.release_scheme_run(db, scheme["_id"], run["_id"])

    counts = store.delete_folder(db, settings, folder)
    assert counts["schemes"] == 1
    assert counts["runs"] == 1
    assert counts["files"] == 1
    assert counts["evaluations"] == 1
    assert store.list_folders(db) == [other]
    assert db.material_filter_schemes.count_documents({}) == 0
    assert db.material_filter_evaluations.count_documents({}) == 0
    with pytest.raises(store.MaterialFilterError):
        store.get_folder(db, folder["_id"])


def test_delete_folder_refuses_while_a_run_is_active(db, settings):
    folder = _folder(db)
    scheme = _scheme(db, settings, folder)
    store.create_run(db, scheme, request_id="busy")
    with pytest.raises(store.MaterialFilterError) as excinfo:
        store.delete_folder(db, settings, folder)
    assert excinfo.value.code == "folder_busy"
    assert store.get_folder(db, folder["_id"])


def test_delete_folder_can_purge_only_its_local_output(db, settings):
    folder = _folder(db)
    scheme = _scheme(db, settings, folder)
    directory = store.scheme_directory(settings, scheme)
    (directory / "passed").mkdir(parents=True)
    (directory / "passed" / "keep.jpg").write_bytes(b"bytes")
    other = _scheme(db, settings, _folder(db, r"\\nas-c\share\x"), name="其他")

    counts = store.delete_folder(db, settings, folder, purge_local=True)
    assert not directory.exists()
    assert store.scheme_directory(settings, other).parent.parent.exists()
    assert counts["purged_directories"]

def test_export_picked_copies_files_with_a_manifest(db, settings, tmp_path):
    folder = _folder(db)
    scheme = _scheme(db, settings, folder)
    directory = store.scheme_directory(settings, scheme)
    (directory / "passed").mkdir(parents=True)
    evaluation = _evaluation(db, scheme)
    output = directory / "passed" / "菜品__abcd.jpg"
    output.write_bytes(b"image-bytes")
    store.update_evaluation(
        db,
        evaluation["_id"],
        {"copy_status": "copied", "output_path": "passed/菜品__abcd.jpg", "output_name": "菜品__abcd.jpg"},
    )
    store.set_manual_pick(db, evaluation, picked=True)

    result = store.export_picked(db, settings, scheme)
    export_dir = Path(result["directory"])
    assert result["count"] == 1
    assert (export_dir / "菜品__abcd.jpg").read_bytes() == b"image-bytes"
    manifest = (export_dir / "manifest.csv").read_text(encoding="utf-8-sig")
    assert "菜品__abcd.jpg" in manifest
    assert "内容哈希" in manifest

    with pytest.raises(store.MaterialFilterError) as excinfo:
        store.set_manual_pick(db, evaluation, picked=False)
        store.export_picked(db, settings, scheme)
    assert excinfo.value.code == "nothing_picked"
