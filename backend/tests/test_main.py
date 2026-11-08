import mongomock

from app.main import _initialize_indexes, app


def test_public_routes_only_expose_whole_image_workflow() -> None:
    paths = set(app.openapi()["paths"])

    assert not any(path == "/api/tasks" or path.startswith("/api/tasks/") for path in paths)
    assert "/api/merchants/{merchant_id}/whole-image/tasks" in paths
    assert "/api/merchants/{merchant_id}/whole-image/tasks/{task_id}/retry" in paths
    assert "/api/merchants/{merchant_id}/whole-image/tasks/{task_id}/edits" in paths
    assert "/api/merchants/{merchant_id}/whole-image/tasks/{task_id}/versions/{version_id}/outputs/{output_id}/select" in paths
    assert not any(path.startswith("/api/poster-edits") for path in paths)
    assert not any(path.startswith("/api/background-replacements") for path in paths)
    assert "/api/merchants/{merchant_id}/task-center" in paths
    assert "/api/merchants/{merchant_id}/prompts/optimize" in paths
    assert "/api/samples" not in paths

def test_initialize_indexes_covers_merchant_scoped_brand_rules() -> None:
    db = mongomock.MongoClient().db
    _initialize_indexes(db)
    keys = {tuple(index["key"].keys()) for index in db.brand_rules.list_indexes()}
    assert ("merchant_id",) in keys