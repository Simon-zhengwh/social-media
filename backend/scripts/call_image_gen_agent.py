"""Call the published image Agent; never call an image gateway directly.

Example (from backend):
  .venv/Scripts/python.exe scripts/call_image_gen_agent.py --image FIRST.png --image SECOND.png
Resume without another generation: add --run-id RUN_ID and use a new --out-dir.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from dotenv import dotenv_values
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AGENT = "86c3a160-54fa-4344-9bc1-6ac56c1e080c"
DEFAULT_SERVER = "https://core-ai-server.connexup-uat.net"


def build_payload(prompt: str, images: list[Path]) -> dict:
    if not prompt.strip():
        raise ValueError("Prompt must not be empty")
    attachments = []
    for path in images:
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("Attachments must be PNG, JPEG or WEBP")
            media_type = Image.MIME[image.format]
            image.verify()
        attachments.append({"type": "IMAGE", "data": base64.b64encode(data).decode("ascii"), "media_type": media_type})
    return {"input": prompt, **({"attachments": attachments} if attachments else {})}


def sanitize(text: str, key: str) -> str:
    if key:
        text = text.replace(key, "[REDACTED]")
    text = re.sub(r"coreai_[A-Za-z0-9_-]+", "[REDACTED]", text)
    return re.sub(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=\s]+", "[INLINE IMAGE OMITTED]", text)


def image_links(output: str) -> list[str]:
    links = re.findall(r"!\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+\"[^\"]*\")?\)", output)
    return list(dict.fromkeys(links))


def origin(url: str) -> tuple:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only HTTPS URLs without embedded credentials are allowed")
    return parsed.scheme, parsed.hostname, parsed.port or 443


def download_image(client: httpx.Client, link: str, base: str, key: str, target: Path) -> dict:
    if link.startswith("data:image/"):
        data = base64.b64decode(link.split(",", 1)[1], validate=True)
    else:
        url = urljoin(base + "/", link)
        for _ in range(6):
            headers = {"Authorization": "Bearer " + key} if origin(url) == origin(base) else {}
            response = client.get(url, headers=headers, follow_redirects=False, timeout=120)
            if response.is_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise RuntimeError("Image redirect has no Location")
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                raise RuntimeError(f"Image download failed: HTTP {response.status_code}")
            data = response.content
            break
        else:
            raise RuntimeError("Too many image redirects")
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(image.format)
        if suffix is None:
            raise RuntimeError("Result is not a supported image")
        result = {"width": image.width, "height": image.height, "format": image.format}
    path = target.with_suffix(suffix)
    with path.open("xb") as stream:
        stream.write(data)
    return {**result, "path": str(path.resolve()), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def execute(client: httpx.Client, base: str, key: str, agent_id: str, payload: dict | None,
            out_dir: Path, *, run_id: str | None = None, timeout: float = 660, poll_interval: float = 3) -> dict:
    origin(base)
    out_dir.mkdir(parents=True, exist_ok=False)
    headers = {"Authorization": "Bearer " + key}
    report = {"agent_id": agent_id, "server": base, "run_id": run_id, "status": "STARTING", "images": []}

    def save():
        (out_dir / "report.json").write_text(sanitize(json.dumps(report, ensure_ascii=False, indent=2), key), encoding="utf-8")

    def request(method: str, path: str, **kwargs) -> dict:
        response = client.request(method, base + path, headers=headers, follow_redirects=False, timeout=30, **kwargs)
        if response.status_code >= 300:
            raise RuntimeError(f"Agent API failed: HTTP {response.status_code}")
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("Agent API returned a non-object response")
        return body

    try:
        if not run_id:
            # No POST retries: even a lost response may have started a billable run.
            report["status"] = "TRIGGER_UNKNOWN"
            save()
            try:
                started = request("POST", f"/api/runs/agent/{agent_id}/trigger", json=payload)
            except httpx.HTTPError as exc:
                raise RuntimeError("Trigger outcome unknown; inspect Recent Runs before submitting again") from exc
            run_id = started.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise RuntimeError("Trigger outcome unknown: response contained no run_id")
            report["run_id"] = run_id
            report["status"] = "PENDING"
            save()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            detail = request("GET", f"/api/runs/{run_id}")
            status = detail.get("status")
            report["status"] = status
            save()
            if status == "COMPLETED":
                output = detail.get("output")
                if not isinstance(output, str):
                    raise RuntimeError("Completed Agent has no text output")
                (out_dir / "response.md").write_text(sanitize(output, key), encoding="utf-8")
                links = image_links(output)
                if not links:
                    raise RuntimeError("Completed Agent returned no image link; inspect response.md and run tools")
                for index, link in enumerate(links, 1):
                    report["images"].append(download_image(client, link, base, key, out_dir / f"image-{index}"))
                    save()
                return report
            if status not in {"PENDING", "RUNNING"}:
                raise RuntimeError(f"Agent run ended or needs attention: {status}; inspect run {run_id}")
            time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
        report["status"] = "WAIT_TIMEOUT"
        raise RuntimeError(f"Wait timed out; resume with --run-id {run_id}; do not submit again")
    except Exception as exc:
        report["error"] = sanitize(str(exc), key) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
        raise
    finally:
        save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", action="append", type=Path, default=[])
    parser.add_argument("--prompt", default="将图一的背景替换为图二")
    parser.add_argument("--agent-id", default=DEFAULT_AGENT)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "output" / "image-gen-skill" / datetime.now().strftime("script-%Y%m%d-%H%M%S"))
    parser.add_argument("--env-file", type=Path, default=ROOT / "backend" / ".env")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--timeout", type=float, default=660)
    args = parser.parse_args()
    key = ""
    try:
        config = {**dotenv_values(args.env_file), **os.environ}
        key = config.get("CORE_AI_API_KEY") or ""
        base = (config.get("CORE_AI_SERVER_BASE_URL") or DEFAULT_SERVER).rstrip("/")
        origin(base)
        if not key:
            raise ValueError("CORE_AI_API_KEY is not configured")
        if args.timeout <= 0:
            raise ValueError("Timeout must be positive")
        if not args.run_id and not args.image:
            raise ValueError("Provide --image inputs, or --run-id to resume")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", args.agent_id) or (args.run_id and not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id)):
            raise ValueError("Invalid agent or run ID")
        if args.out_dir.exists():
            raise FileExistsError("Output directory already exists; choose a new --out-dir")
        payload = None if args.run_id else build_payload(args.prompt, args.image)
        summary = {"server": base, "agent_id": args.agent_id, "run_id": args.run_id,
                   "images": [str(p.resolve()) for p in args.image], "out_dir": str(args.out_dir.resolve())}
        print(json.dumps(summary, ensure_ascii=True), flush=True)
        if args.dry_run:
            print("Dry run: validated inputs; no request sent and no files written.")
            return 0
        with httpx.Client() as client:
            report = execute(client, base, key, args.agent_id, payload, args.out_dir, run_id=args.run_id, timeout=args.timeout)
        print(json.dumps(report, ensure_ascii=True))
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, (RuntimeError, ValueError, FileExistsError)) else type(exc).__name__
        print(sanitize(message, key))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
