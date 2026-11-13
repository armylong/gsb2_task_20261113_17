"""主 Web 服务：REST API、静态页面、CSV/JSON 导出。"""
from __future__ import annotations

import json
import mimetypes
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .db import Database
from .diff import diff_rounds
from .engine import Engine
from .export import export_csv, export_json
from .utils import normalize_origin, get_nested


def row_to_dict(row):
    return dict(row) if row is not None else None


class App:
    """组装数据库、限速器、采集引擎、Web 服务。"""

    def __init__(self, db_path: str, web_dir: str):
        self.db = Database(db_path)
        from .utils import HostRateLimiter
        self.limiter = HostRateLimiter()
        self.engine = Engine(self.db, self.limiter)
        self.web_dir = web_dir
        self.server: ThreadingHTTPServer | None = None
        self.test_site_base: str | None = None  # 由启动入口注入
        # 已配置过的网站限速参数（重启后恢复）
        for t in self.db.list_tasks():
            self.limiter.configure(t["origin"], t["min_interval"])

    def start(self, host: str = "127.0.0.1", port: int = 8000) -> str:
        self.engine.start()
        handler = make_handler(self)
        self.server = ThreadingHTTPServer((host, port), handler)
        thread = __import__("threading").Thread(
            target=self.server.serve_forever, daemon=True)
        thread.start()
        return f"http://{host}:{self.server.server_address[1]}"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.engine.shutdown()
        self.db.close()


def make_handler(app: App):
    db: Database = app.db
    engine: Engine = app.engine

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        # ---------- 基础收发 ----------
        def _send_json(self, code: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: str) -> None:
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            if not raw.strip():
                return {}
            return json.loads(raw)

        def _error(self, code: int, message: str) -> None:
            self._send_json(code, {"error": message})

        # ---------- 路由 ----------
        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                path, qs = parsed.path, parse_qs(parsed.query)
                if path == "/api/runtime":
                    return self._send_json(200, {
                        "test_site_base": app.test_site_base})
                if path == "/api/tasks":
                    return self._list_tasks()
                if path.startswith("/api/tasks/"):
                    return self._task_get(path, qs)
                if path.startswith("/api/rounds/"):
                    return self._round_get(path, qs)
                return self._static(path)
            except FileNotFoundError:
                self._error(404, "资源不存在")
            except (ValueError, RuntimeError) as e:
                self._error(400, str(e))
            except Exception as e:
                self._error(500, f"服务器错误：{e}")

        def do_POST(self):
            try:
                path = urlparse(self.path).path
                if path == "/api/tasks":
                    return self._create_task()
                # 注意顺序：pause/resume 是 /api/rounds/... 下的具体动作，
                # 必须在通用的 /rounds 后缀判断之前匹配
                if path.endswith("/pause"):
                    return self._control_round(path, "pause")
                if path.endswith("/resume"):
                    return self._control_round(path, "resume")
                if path.endswith("/rounds"):
                    return self._start_round(path)
                self._error(404, "接口不存在")
            except (ValueError, RuntimeError) as e:
                self._error(400, str(e))
            except Exception as e:
                self._error(500, f"服务器错误：{e}")

        # ---------- 任务 ----------
        def _list_tasks(self):
            tasks = [row_to_dict(r) for r in db.list_tasks()]
            for t in tasks:
                t["fields"] = json.loads(t.pop("fields_json"))
            self._send_json(200, tasks)

        def _create_task(self):
            body = self._read_json()
            name = (body.get("name") or "").strip()
            url = (body.get("entry_url") or "").strip()
            if not name:
                raise ValueError("任务名称不能为空")
            try:
                origin = normalize_origin(url)
            except ValueError as e:
                raise ValueError(str(e))
            max_concurrency = max(1, int(body.get("max_concurrency", 1)))
            min_interval = max(0.0, float(body.get("min_interval", 1.0)))
            max_retries = max(0, int(body.get("max_retries", 3)))
            fields = body.get("fields") or [
                {"path": "id", "column": "id"}]
            if not isinstance(fields, list) or any(
                    "path" not in f for f in fields):
                raise ValueError("字段配置必须是包含 path 的数组")
            task_id = db.create_task(name, url, origin, max_concurrency,
                                     min_interval, max_retries, fields)
            app.limiter.configure(origin, min_interval)
            self._send_json(201, row_to_dict(db.get_task(task_id)))

        def _task_get(self, path: str, qs):
            parts = [p for p in path.split("/") if p]
            # /api/tasks/{id}
            if len(parts) == 3:
                task = db.get_task(int(parts[2]))
                if task is None:
                    return self._error(404, "任务不存在")
                data = row_to_dict(task)
                data["fields"] = json.loads(data.pop("fields_json"))
                data["rounds"] = [row_to_dict(r) for r in db.list_rounds(task["id"])]
                return self._send_json(200, data)
            task_id = int(parts[2])
            sub = parts[3]
            if sub == "diff" and len(parts) == 5:
                return self._send_json(200, diff_rounds(db, task_id, int(parts[4])))
            self._error(404, "接口不存在")

        def _start_round(self, path: str):
            # /api/tasks/{id}/rounds
            parts = [p for p in path.split("/") if p]
            task_id = int(parts[2])
            round_id = engine.start_round(task_id)
            self._send_json(201, row_to_dict(db.get_round(round_id)))

        def _control_round(self, path: str, action: str):
            # /api/rounds/{id}/pause|resume
            parts = [p for p in path.split("/") if p]
            round_id = int(parts[2])
            if action == "pause":
                engine.pause_round(round_id)
            else:
                engine.resume_round(round_id)
            self._send_json(200, row_to_dict(db.get_round(round_id)))

        # ---------- 轮次详情 ----------
        def _round_get(self, path: str, qs):
            parts = [p for p in path.split("/") if p]
            round_id = int(parts[2])
            round_row = db.get_round(round_id)
            if round_row is None:
                return self._error(404, "轮次不存在")
            sub = parts[3] if len(parts) > 3 else ""

            if sub == "records":
                page = max(1, int(qs.get("page", ["1"])[0]))
                size = min(200, max(1, int(qs.get("size", ["20"])[0])))
                total = round_row["record_count"]
                rows = db.list_records(round_id, size, (page - 1) * size)
                task = db.get_task(round_row["task_id"])
                fields = json.loads(task["fields_json"])
                records = []
                for r in rows:
                    data = json.loads(r["data_json"])
                    projected = {}
                    for f in fields:
                        value = get_nested(data, f["path"])
                        projected[f.get("column") or f["path"]] = "" if value is None else value
                    records.append({"record_id": r["record_id"],
                                    "raw": data, "row": projected})
                return self._send_json(200, {
                    "page": page, "size": size, "total": total,
                    "pages": (total + size - 1) // size, "records": records})

            if sub == "failures":
                return self._send_json(200,
                    [row_to_dict(r) for r in db.list_failures(round_id)])

            if sub == "diff":
                return self._send_json(200,
                    diff_rounds(db, round_row["task_id"], round_id))

            if sub == "export":
                fmt = qs.get("format", ["csv"])[0]
                task = db.get_task(round_row["task_id"])
                fields = json.loads(task["fields_json"])
                stamp = time.strftime("%Y%m%d_%H%M%S")
                if fmt == "json":
                    body = export_json(db, round_id)
                    ctype = "application/json; charset=utf-8"
                    fname = f"round{round_row['round_no']}_{stamp}.json"
                else:
                    body = export_csv(db, round_id, fields)
                    ctype = "text/csv; charset=utf-8"
                    fname = f"round{round_row['round_no']}_{stamp}.csv"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition",
                                 f"attachment; filename*=UTF-8''{fname}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if sub == "record":
                # /api/rounds/{id}/record/{rid}
                rid = parts[4]
                history = db.get_record_history(round_row["task_id"], rid)
                out = []
                for h in history:
                    out.append({"round_id": h["round_id"],
                                "round_no": h["round_no"],
                                "round_status": h["round_status"],
                                "data": json.loads(h["data_json"]),
                                "updated_at": h["updated_at"]})
                return self._send_json(200, out)

            # 不带子路径：轮次基本信息
            return self._send_json(200, row_to_dict(round_row))

        # ---------- 静态页面 ----------
        def _static(self, path: str):
            if path == "/":
                path = "/index.html"
            # 防目录穿越
            rel = os.path.normpath(path.lstrip("/"))
            full = os.path.join(app.web_dir, rel)
            if not full.startswith(os.path.abspath(app.web_dir) + os.sep) \
                    and full != os.path.abspath(app.web_dir):
                return self._error(403, "禁止访问")
            if not os.path.isfile(full):
                return self._error(404, "页面不存在")
            self._send_file(full)

    return Handler
