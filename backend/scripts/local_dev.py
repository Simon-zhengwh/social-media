"""Explicit local startup; --check only reads MongoDB and filesystem metadata."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from typing import Any

from pymongo.uri_parser import parse_uri

from app.config import BACKEND_DIR, PROJECT_DIR, Settings, get_settings, local_bypass_enabled
from app.db import create_client


class LocalDevError(Exception):
    pass


def validate_local_settings(settings: Settings) -> None:
    if not local_bypass_enabled(settings):
        raise LocalDevError("本地启动要求 AM_BYPASS_LOCAL=1，且 SOCIAL_MEDIA_ORIGIN 指向本机。")
    if not settings.am_bypass_merchant_id.strip():
        raise LocalDevError("请配置 AM_BYPASS_MERCHANT_ID，指定现有数据所属的商户。")
    # This entry point is specifically for local demo data, never a deployed database.
    try:
        if not settings.mongodb_uri.startswith("mongodb://"):
            raise ValueError
        hosts = parse_uri(settings.mongodb_uri)["nodelist"]
        if not hosts or any(host not in {"127.0.0.1", "localhost", "::1"} for host, _ in hosts):
            raise ValueError
    except Exception:
        raise LocalDevError("本地启动要求 MONGODB_URI 只连接本机 MongoDB。") from None


def check_local_data(settings: Settings, db: Any, *, recover_interrupted: bool = False) -> dict[str, Any]:
    validate_local_settings(settings)
    objects = (settings.storage_path / "objects").resolve()
    if not objects.is_dir():
        raise LocalDevError("STORAGE_DIR 下没有 objects 目录，请指向现有 demo 的 local_media 目录。")
    merchant_id = settings.am_bypass_merchant_id.strip()
    query = {"merchant_id": merchant_id}
    counts = {name: db[name].count_documents(query) for name in ("assets", "brands", "whole_tasks")}
    if not any(counts.values()) and db.merchant_profiles.find_one({"_id": merchant_id}) is None:
        raise LocalDevError("数据库中找不到该商户，请核对 AM_BYPASS_MERCHANT_ID 和数据库名称。")

    # App startup recovers jobs across the raw DB, so check every merchant here.
    active_queries = {
        "whole_tasks": {"$or": [{"status": {"$in": ["queued", "running"]}}, {"edit.status": "running"}]},
        "poster_edit_tasks": {"status": {"$in": ["queued", "running"]}},
        "background_replace_tasks": {"whole_task_id": {"$type": "string", "$ne": ""}, "status": {"$in": ["queued", "running"]}},
        "agent_test_runs": {"status": {"$in": ["queued", "preparing", "triggering", "running"]}},
        "material_filter_runs": {"status": {"$in": ["queued", "running"]}},
        "material_filter_syncs": {"status": {"$in": ["queued", "running"]}},
    }
    active_jobs = any(db[name].find_one(active) is not None for name, active in active_queries.items())
    if active_jobs and not recover_interrupted:
        raise LocalDevError("数据库里存在运行中任务。请等待任务结束并停止旧后端；如旧后端已退出，可用 -Recover 恢复中断任务。此次不会修改任务。")

    def file_exists(key: Any) -> bool:
        if not isinstance(key, str) or not key:
            return False
        path = (objects / key).resolve()
        return path.is_relative_to(objects) and path.is_file()

    missing_assets = sum(
        not file_exists(doc.get("prepared_path") or doc.get("path"))
        for doc in db.assets.find(query, {"prepared_path": 1, "path": 1})
    )
    missing_artifacts = sum(not file_exists(doc.get("path")) for doc in db.whole_artifacts.find(query, {"path": 1}))
    warnings = []
    if active_jobs:
        warnings.append("已选择恢复中断任务；请确保旧后端已停止。实际启动后将处理这些记录：普通生图/改图标记中断，已有 Agent run 按原流程恢复。")
    if missing_assets or missing_artifacts:
        warnings.append(f"历史记录中缺少 {missing_assets} 个素材文件、{missing_artifacts} 个 HTML/ZIP 文件；不会自动删除或重建记录。")
    if not (settings.storage_path / "agent-test-cases/manifest.json").is_file():
        warnings.append("未准备 Agent 固定测试素材，不影响海报工作台。")
    if not settings.core_ai_api_key or settings.core_ai_api_key == "replace_me":
        warnings.append("未配置 CORE_AI_API_KEY，生图、改图和提示词优化暂不可用。")
    if not settings.image_host_upload_token or settings.image_host_upload_token == "replace_me":
        warnings.append("未配置 IMAGE_HOST_UPLOAD_TOKEN，提示词优化读取本地图片可能失败。")
    return {**counts, "missing_assets": missing_assets, "missing_artifacts": missing_artifacts, "warnings": warnings}


def require_free_ports(*ports: int) -> None:
    if len(set(ports)) != len(ports):
        raise LocalDevError("前后端必须使用不同端口。")
    for port in ports:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise LocalDevError(f"端口 {port} 已被占用，请先停止旧服务，或使用 -BackendPort / -FrontendPort 指定端口。") from None


def run_servers(backend_port: int, frontend_port: int) -> None:
    require_free_ports(backend_port, frontend_port)
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm or not (PROJECT_DIR / "frontend/node_modules/vite").is_dir():
        raise LocalDevError("请先安装 Node.js，并在 frontend 下执行 npm install。")
    env = dict(os.environ)
    env.update({
        "SOCIAL_MEDIA_ORIGIN": f"http://127.0.0.1:{frontend_port}",
        "SOCIAL_MEDIA_URL": f"http://127.0.0.1:{frontend_port}",
        "SOCIAL_MEDIA_API_PROXY_TARGET": f"http://127.0.0.1:{backend_port}",
    })
    backend_cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(backend_port), "--workers", "1"]
    # npm's Windows entry point is a batch file. No interpolated user text enters cmd.
    frontend_cmd = (
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", f'npm run dev -- --host 127.0.0.1 --port {frontend_port} --strictPort']
        if os.name == "nt" else [npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(frontend_port), "--strictPort"]
    )
    processes = []
    print(f"正在启动：http://127.0.0.1:{frontend_port}  API：http://127.0.0.1:{backend_port}/docs", flush=True)
    print("按 Ctrl+C 停止本次启动的前后端。共享数据库时，请勿同时运行 demo 后端。", flush=True)
    try:
        for command, cwd in [(backend_cmd, BACKEND_DIR), (frontend_cmd, PROJECT_DIR / "frontend")]:
            processes.append(subprocess.Popen(
                command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            ))
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        raise LocalDevError("前端或后端已退出，请查看上方启动日志。")
    except KeyboardInterrupt:
        print("\n正在停止本地服务……", flush=True)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                else:
                    import signal
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=BACKEND_DIR / ".env.local")
    parser.add_argument("--check", action="store_true", help="只读检查，不启动服务或调用模型")
    parser.add_argument("--recover", action="store_true", help="确认旧后端已停止后，允许处理遗留的运行中任务")
    parser.add_argument("--backend-port", type=int, choices=range(1, 65536), default=8000, metavar="PORT")
    parser.add_argument("--frontend-port", type=int, choices=range(1, 65536), default=5173, metavar="PORT")
    args = parser.parse_args(argv)
    try:
        env_file = args.env_file.resolve()
        if not env_file.is_file():
            raise LocalDevError("缺少本地配置。请复制 backend/.env.local.example 为 backend/.env.local，并填写现有数据库、商户和图片路径。")
        os.environ["SOCIAL_MEDIA_ENV_FILE"] = str(env_file)
        get_settings.cache_clear()
        try:
            settings = get_settings()
        except ValueError:
            raise LocalDevError("本地配置格式无效，请检查配置值；为保护凭据，不输出原始配置。") from None
        validate_local_settings(settings)
        client = create_client(settings.mongodb_uri)
        try:
            try:
                client.admin.command("ping")
            except Exception:
                raise LocalDevError("无法连接本机 MongoDB，请确认已有数据库服务和 MONGODB_URI。") from None
            report = check_local_data(settings, client[settings.mongodb_database], recover_interrupted=args.recover)
        finally:
            client.close()
        print(f"本地模式：数据库 {settings.mongodb_database}；商户 {settings.am_bypass_merchant_id}", flush=True)
        print(f"图片目录：{settings.storage_path}", flush=True)
        print(f"素材 {report['assets']}，品牌风格 {report['brands']}，海报任务 {report['whole_tasks']}", flush=True)
        for warning in report["warnings"]:
            print(f"提示：{warning}", flush=True)
        if args.check:
            print("只读检查通过；未启动服务、修改数据或调用模型。", flush=True)
        else:
            run_servers(args.backend_port, args.frontend_port)
        return 0
    except (LocalDevError, OSError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
