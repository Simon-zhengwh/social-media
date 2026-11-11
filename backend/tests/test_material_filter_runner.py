from __future__ import annotations

import io
import threading
from pathlib import Path

import mongomock
import pytest
from PIL import Image

from app.config import Settings
from app.services import material_filter_files as mf
from app.services import material_filter_runner as runner
from app.services import material_filters as store
from app.services.core_ai import CoreAIError
from app.services.scoped_db import ScopedDatabase

RULES = [
    {"id": "empty_seating_scene", "text": "不能出现空座位场景", "interpretation": "主体为空置就餐区才违反"},
    {"id": "crab_legs", "text": "不能出现螃蟹腿", "interpretation": "整蟹上可见的腿也算"},
]


def _jpeg(color=(180, 90, 50), size=(64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _payload(decision="pass"):
    verdict = {"pass": "pass", "fail": "fail", "uncertain": "uncertain"}[decision]
    return {
        "name": "红烧肉砂锅特写",
        "category": "食物",
        "tags": ["红烧肉", "砂锅", "浓汁", "热菜"],
        "description": "一只砂锅装着红烧肉。",
        "rules": [
            {"id": "empty_seating_scene", "verdict": verdict, "reason": "逐图判断依据。"},
            {"id": "crab_legs", "verdict": "pass", "reason": "未见蟹腿。"},
        ],
        "decision": decision,
    }


class FakeCoreAI:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def chat_json(self, messages, *, model=None, timeout_seconds=None):
        self.calls += 1
        if self.responses:
            value = self.responses.pop(0)
        else:
            value = _payload()
        if isinstance(value, Exception):
            raise value
        return value


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
        material_filter_concurrency=2,
    )


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source"
    path.mkdir()
    return path


def _folder(db, source: Path):
    timestamp = store.now()
    db.material_filter_folders.insert_one(
        {
            "_id": "mfolder_test",
            "display_name": "照片",
            "original_input": str(source),
            "source_root": str(source),
            "canonical_source_key": "test-key",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )
    return store.get_folder(db, "mfolder_test")


def _scheme(db, settings, source, *, name="默认方案", rules=None):
    return store.create_scheme(
        db,
        settings,
        _folder(db, source),
        name=name,
        rules=rules or RULES,
    )


def _scan(db, settings, client, scheme, *, request_id="req", resume=False, stop_event=None, limit=None):
    run, _ = store.create_run(db, scheme, request_id=request_id, limit=limit)
    return runner.run_scan(
        db, settings, client, run["_id"], resume=resume, stop_event=stop_event
    )


def test_first_scan_evaluates_copies_and_writes_previews(db, settings, source):
    (source / "a.jpg").write_bytes(_jpeg())
    (source / "b.jpg").write_bytes(_jpeg((20, 120, 200)))
    scheme = _scheme(db, settings, source)
    client = FakeCoreAI()

    run = _scan(db, settings, client, scheme)

    assert run["status"] == "succeeded"
    assert client.calls == 2
    assert run["counters"]["scanned"] == 2
    assert run["counters"]["added"] == 2
    assert run["counters"]["passed"] == 2
    assert run["counters"]["copied"] == 2
    evaluations = list(db.material_filter_evaluations.find({}))
    assert len(evaluations) == 2
    for evaluation in evaluations:
        assert evaluation["copy_status"] == "copied"
        assert evaluation["output_path"].startswith("passed/")
        passed = store.scheme_directory(settings, scheme) / evaluation["output_path"]
        assert passed.is_file()
        assert passed.read_bytes() == (source / evaluation["original_name"]).read_bytes()
        preview = store.scheme_directory(settings, scheme) / evaluation["preview_path"]
        assert preview.is_file()


def test_second_scan_without_changes_calls_no_model(db, settings, source):
    (source / "a.jpg").write_bytes(_jpeg())
    (source / "b.jpg").write_bytes(_jpeg((10, 10, 10)))
    scheme = _scheme(db, settings, source)
    client = FakeCoreAI()
    _scan(db, settings, client, scheme, request_id="first")
    assert client.calls == 2

    run = _scan(db, settings, client, store.get_scheme(db, scheme["_id"]), request_id="second")
    assert client.calls == 2
    assert run["counters"]["skipped"] == 2
    assert db.material_filter_evaluations.count_documents({}) == 2


def test_incremental_scan_only_judges_new_files(db, settings, source):
    (source / "a.jpg").write_bytes(_jpeg())
    scheme = _scheme(db, settings, source)
    client = FakeCoreAI()
    _scan(db, settings, client, scheme, request_id="first")

    (source / "b.jpg").write_bytes(_jpeg((30, 200, 30)))
    run = _scan(db, settings, client, store.get_scheme(db, scheme["_id"]), request_id="second")
    assert client.calls == 2
    assert run["counters"]["added"] == 1
    assert run["counters"]["skipped"] == 1
    assert db.material_filter_evaluations.count_documents({}) == 2


def test_touched_but_identical_file_reuses_the_verdict(db, settings, source):
    path = source / "a.jpg"
    path.write_bytes(_jpeg())
    scheme = _scheme(db, settings, source)
    client = FakeCoreAI()
    _scan(db, settings, client, scheme, request_id="first")

    # Same bytes, later mtime: the hash check avoids a second paid request.
    path.write_bytes(path.read_bytes())
    run = _scan(db, settings, client, store.get_scheme(db, scheme["_id"]), request_id="second")
    assert client.calls == 1
    assert run["counters"]["skipped"] == 1
    assert db.material_filter_evaluations.count_documents({}) == 1


def test_changed_content_is_judged_again(db, settings, source):
    path = source / "a.jpg"
    path.write_bytes(_jpeg((10, 10, 10)))
    scheme = _scheme(db, settings, source)
    client = FakeCoreAI()
    _scan(db, settings, client, scheme, request_id="first")
    first = db.material_filter_evaluations.find_one({})

    path.write_bytes(_jpeg((250, 10, 10)))
    run = _scan(db, settings, client, store.get_scheme(db, scheme["_id"]), request_id="second")
    assert client.calls == 2
    assert run["counters"]["changed"] == 1
    evaluations = list(db.material_filter_evaluations.find({}).sort("created_at", 1))
    assert len(evaluations) == 2
    assert evaluations[0]["source_sha256"] != evaluations[1]["source_sha256"]
    assert evaluations[0]["_id"] == first["_id"]


def test_fail_and_uncertain_are_settled_but_never_copied(db, settings, source):
    (source / "fail.jpg").write_bytes(_jpeg((40, 40, 40)))
    (source / "maybe.jpg").write_bytes(_jpeg((60, 60, 60)))
    scheme = _scheme(db, settings, source)
    client = FakeCoreAI(_payload("fail"), _payload("uncertain"))
    run = _scan(db, settings, client, scheme, request_id="first")
    assert run["counters"]["failed"] == 1
    assert run["counters"]["uncertain"] == 1
    assert run["counters"]["copied"] == 0
    assert all(item["copy_status"] == "none" for item in db.material_filter_evaluations.find({}))
    assert not (store.scheme_directory(settings, scheme) / "passed").exists()

    run = _scan(db, settings, client, store.get_scheme(db, scheme["_id"]), request_id="second")
    assert client.calls == 2
    assert run["counters"]["skipped"] == 2


def test_technical_failure_is_retried_by_the_next_run(db, settings, source):
    (source / "a.jpg").write_bytes(_jpeg())
    scheme = _scheme(db, settings, source)
    failing = FakeCoreAI(CoreAIError("gateway_timeout", "结果未知"))
    run = _scan(db, settings, failing, scheme, request_id="first")
    assert run["counters"]["errors"] == 1
    evaluation = db.material_filter_evaluations.find_one({})
    assert evaluation["decision"] == "error"
    assert evaluation["copy_status"] == "none"

    retry = FakeCoreAI()
    run = _scan(db, settings, retry, store.get_scheme(db, scheme["_id"]), request_id="second")
    assert retry.calls == 1
    assert run["counters"]["passed"] == 1
    assert db.material_filter_evaluations.count_documents({"decision": "error"}) == 0
    assert db.material_filter_evaluations.count_documents({"decision": "pass"}) == 1


def test_same_name_in_different_subdirectories_is_independent(db, settings, source):
    (source / "one").mkdir()
    (source / "two").mkdir()
    (source / "one" / "dish.jpg").write_bytes(_jpeg((10, 20, 30)))
    (source / "two" / "dish.jpg").write_bytes(_jpeg((200, 150, 10)))
    scheme = _scheme(db, settings, source)
    client = FakeCoreAI()
    run = _scan(db, settings, client, scheme, request_id="first")
    assert client.calls == 2
    assert run["counters"]["added"] == 2
    paths = sorted(item["relative_path"] for item in db.material_filter_evaluations.find({}))
    assert paths == ["one/dish.jpg", "two/dish.jpg"]
    assert all(item["copy_status"] == "copied" for item in db.material_filter_evaluations.find({}))


def test_a_new_scheme_does_not_reuse_another_schemes_verdict(db, settings, source):
    (source / "a.jpg").write_bytes(_jpeg())
    folder = _folder(db, source)
    first = store.create_scheme(db, settings, folder, name="A", rules=RULES)
    second = store.create_scheme(
        db, settings, folder, name="B", rules=[{"id": "empty_seating_scene", "text": "只查空座位"}]
    )
    client = FakeCoreAI()
    _scan(db, settings, client, first, request_id="a")
    _scan(db, settings, client, second, request_id="b")
    assert client.calls == 2
    assert db.material_filter_evaluations.count_documents({"scheme_id": first["_id"]}) == 1
    assert db.material_filter_evaluations.count_documents({"scheme_id": second["_id"]}) == 1


def test_copy_failure_keeps_the_pass_and_can_be_retried(db, settings, source, monkeypatch):
    (source / "a.jpg").write_bytes(_jpeg())
    scheme = _scheme(db, settings, source)
    real_copy = mf.copy_approved_bytes

    def broken(*args, **kwargs):
        raise mf.MaterialFilterFileError("output_conflict", "模拟复制失败", 409)

    monkeypatch.setattr(mf, "copy_approved_bytes", broken)
    run = _scan(db, settings, FakeCoreAI(), scheme, request_id="first")
    assert run["counters"]["copy_errors"] == 1
    evaluation = db.material_filter_evaluations.find_one({})
    assert evaluation["decision"] == "pass"
    assert evaluation["copy_status"] == "error"

    monkeypatch.setattr(mf, "copy_approved_bytes", real_copy)
    reviewed = store.review_evaluation(
        db,
        settings,
        scheme,
        evaluation,
        decision="pass",
        source_sha256=evaluation["source_sha256"],
        request_id="review-1",
    )
    assert reviewed["copy_status"] == "copied"
    assert (store.scheme_directory(settings, scheme) / reviewed["output_path"]).is_file()


def test_interrupted_run_resumes_only_when_asked(db, settings, source):
    (source / "a.jpg").write_bytes(_jpeg())
    (source / "b.jpg").write_bytes(_jpeg((50, 50, 50)))
    scheme = _scheme(db, settings, source)
    client = FakeCoreAI()
    stop = threading.Event()
    stop.set()
    run = _scan(db, settings, client, scheme, request_id="first", stop_event=stop)
    assert run["status"] == "interrupted"
    assert client.calls == 0

    resumed = _scan(
        db, settings, client, store.get_scheme(db, scheme["_id"]), resume=True
    )
    assert resumed["status"] == "succeeded"
    assert client.calls == 2


def test_deleting_a_source_file_keeps_local_copies(db, settings, source):
    path = source / "a.jpg"
    path.write_bytes(_jpeg())
    scheme = _scheme(db, settings, source)
    _scan(db, settings, FakeCoreAI(), scheme, request_id="first")
    evaluation = db.material_filter_evaluations.find_one({})
    passed = store.scheme_directory(settings, scheme) / evaluation["output_path"]
    path.unlink()
    _scan(db, settings, FakeCoreAI(), store.get_scheme(db, scheme["_id"]), request_id="second")
    assert passed.is_file()


def test_recheck_rereads_the_image_and_updates_the_model_result(db, settings, source):
    (source / "a.jpg").write_bytes(_jpeg())
    scheme = _scheme(db, settings, source)
    client = FakeCoreAI(_payload("uncertain"))
    _scan(db, settings, client, scheme, request_id="first")
    evaluation = db.material_filter_evaluations.find_one({})
    assert evaluation["effective_decision"] == "uncertain"

    updated = runner.recheck_evaluation(db, settings, FakeCoreAI(_payload("pass")), scheme, evaluation)
    assert updated["decision"] == "pass"
    assert updated["effective_decision"] == "pass"
    assert updated["copy_status"] == "copied"

def test_run_counters_are_written_after_every_file(db, settings, source, monkeypatch):
    (source / "a.jpg").write_bytes(_jpeg())
    (source / "b.jpg").write_bytes(_jpeg((30, 90, 30)))
    scheme = _scheme(db, settings, source)
    snapshots: list[dict] = []
    original = store.set_run_counters

    def record(database, run_id, counters):
        snapshots.append(dict(counters))
        original(database, run_id, counters)

    monkeypatch.setattr(store, "set_run_counters", record)
    _scan(db, settings, FakeCoreAI(), scheme, request_id="progress")

    # One write at scan start, then one after every processed file.
    assert len(snapshots) == 3
    assert snapshots[0]["scanned"] == 2
    assert snapshots[1]["added"] + snapshots[1]["skipped"] >= 1
    assert snapshots[-1]["passed"] == 2
