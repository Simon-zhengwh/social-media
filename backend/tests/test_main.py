from app.main import app


def test_public_routes_only_expose_whole_image_workflow() -> None:
    paths = set(app.openapi()["paths"])

    assert not any(path == "/api/tasks" or path.startswith("/api/tasks/") for path in paths)
    assert "/api/whole-image/tasks" in paths
    assert "/api/whole-image/tasks/{task_id}/edits" in paths
    assert "/api/prompts/optimize" in paths
