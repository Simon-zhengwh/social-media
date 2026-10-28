import base64
import io
import json

import httpx
import pytest
from PIL import Image

from scripts.call_image_gen_agent import build_payload, execute, download_image
from scripts import call_image_gen_agent as script


def png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (12, 16), color).save(stream, "PNG")
    return stream.getvalue()


def test_payload_preserves_attachment_order_and_bytes(tmp_path):
    paths = [tmp_path / "first.png", tmp_path / "second.png"]
    for path, color in zip(paths, ["red", "blue"]):
        path.write_bytes(png(color))
    payload = build_payload("replace background", paths)
    assert payload["input"] == "replace background"
    assert [base64.b64decode(a["data"]) for a in payload["attachments"]] == [p.read_bytes() for p in paths]
    assert all(a["type"] == "IMAGE" and a["media_type"] == "image/png" for a in payload["attachments"])


def test_execute_saves_result_and_does_not_send_key_to_cdn(tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.host == "cdn.example.com":
            assert "Authorization" not in request.headers
            return httpx.Response(200, content=png())
        assert request.headers["Authorization"] == "Bearer secret-test"
        if request.method == "POST":
            assert json.loads(request.content) == {"input": "edit"}
            return httpx.Response(202, json={"run_id": "run-1"})
        return httpx.Response(200, json={"status": "COMPLETED", "output": "![result](https://cdn.example.com/result.png)"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = execute(client, "https://core.example.com", "secret-test", "agent-1", {"input": "edit"}, tmp_path / "run")
    assert report["status"] == "COMPLETED"
    assert report["run_id"] == "run-1"
    assert (tmp_path / "run" / "image-1.png").read_bytes() == png()
    assert len([c for c in calls if c[0] == "POST"]) == 1
    assert "secret-test" not in (tmp_path / "run" / "report.json").read_text()


@pytest.mark.parametrize("status", ["FAILED", "TIMEOUT", "CANCELLED", "SKIPPED"])
def test_failed_run_is_not_resubmitted(tmp_path, status):
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json={"status": status, "error": "secret-test"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match=status):
            execute(client, "https://core.example.com", "secret-test", "agent", None, tmp_path / "run", run_id="existing")
    assert methods == ["GET"]
    assert "secret-test" not in (tmp_path / "run" / "report.json").read_text()


def test_trigger_timeout_is_not_retried(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="unknown"):
            execute(client, "https://core.example.com", "key", "agent", {"input": "edit"}, tmp_path / "run")
    assert len(calls) == 1
    assert json.loads((tmp_path / "run" / "report.json").read_text())["status"] == "TRIGGER_UNKNOWN"


def test_completed_without_image_is_not_success(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "COMPLETED", "output": "Sorry, failed."}))) as client:
        with pytest.raises(RuntimeError, match="no image"):
            execute(client, "https://core.example.com", "key", "agent", None, tmp_path / "run", run_id="existing")


def test_existing_output_is_rejected_before_request(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("must not request"))) as client:
        with pytest.raises(FileExistsError):
            execute(client, "https://core.example.com", "key", "agent", {}, tmp_path)


def test_download_error_is_not_saved_as_image(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403))) as client:
        with pytest.raises(RuntimeError, match="403"):
            download_image(client, "https://cdn.example.com/out.png", "https://core.example.com", "key", tmp_path / "image")
    assert not list(tmp_path.iterdir())


def test_redirect_drops_authorization(tmp_path):
    def handler(request):
        if request.url.host == "core.example.com":
            assert request.headers["Authorization"] == "Bearer key"
            return httpx.Response(302, headers={"Location": "https://cdn.example.com/image.png"})
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=png())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = download_image(client, "/image", "https://core.example.com", "key", tmp_path / "image")
    assert result["width"] == 12 and result["height"] == 16


def test_poll_timeout_keeps_run_id_without_resubmitting(tmp_path, monkeypatch):
    ticks = iter([0, 0, 2, 2])
    monkeypatch.setattr(script.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(script.time, "sleep", lambda _: None)
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json={"run_id": "run-1"} if request.method == "POST" else {"status": "RUNNING"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="--run-id run-1"):
            execute(client, "https://core.example.com", "key", "agent", {"input": "edit"}, tmp_path / "run", timeout=1)
    assert methods == ["POST", "GET"]
    report = json.loads((tmp_path / "run" / "report.json").read_text())
    assert report["run_id"] == "run-1" and report["status"] == "WAIT_TIMEOUT"


def test_dry_run_makes_no_request_or_files(tmp_path, monkeypatch, capsys):
    source = tmp_path / "input.png"
    source.write_bytes(png())
    output = tmp_path / "result"
    monkeypatch.setenv("CORE_AI_API_KEY", "test-secret")
    monkeypatch.setenv("CORE_AI_SERVER_BASE_URL", "https://core.example.com")
    monkeypatch.setattr("sys.argv", ["script", "--dry-run", "--image", str(source), "--out-dir", str(output)])
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("must not create network client"))
    assert script.main() == 0
    assert not output.exists()
    assert "test-secret" not in capsys.readouterr().out
