import httpx
import pytest

from app.services.core_ai import CoreAIClient, CoreAIError


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "SKIPPED", "UNKNOWN"])
def test_single_poll_never_triggers_agent(settings, status):
    paths = []

    def handler(request):
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer test-key"
        paths.append(request.url.path)
        return httpx.Response(200, json={"status": status, "output": "![替换结果](https://example.com/final.png)", "error": "upstream failed"})

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        if status in {"PENDING", "RUNNING"}:
            assert client.poll_agent_detail("run-old") is None
        elif status == "COMPLETED":
            assert client.poll_agent_detail("run-old").output.startswith("![替换结果]")
        else:
            with pytest.raises(CoreAIError):
                client.poll_agent_detail("run-old")
    finally:
        client.close()
    assert paths == ["/api/runs/run-old"]
