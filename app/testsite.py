"""内置本地测试网站。

提供返回 JSON 列表的分页接口，支持在线切换场景与修改数据：
  normal       正常翻页（含中文、嵌套 author.name）
  loop         记录重复 + 下一页绕回已采页面
  ratelimit    先返回 429（带 Retry-After），随后恢复正常
  error        一直返回 HTTP 500
  redirect     302 自动跳转到“外站”（另一个端口的服务）
  offsite_next 本页正常，但 next 字段指向外站地址

数据保存在 state_path（JSON 文件），重启演示网站后修改仍在。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SEED_RECORDS = [
    {"id": "p1", "title": "机械键盘评测", "price": 399,
     "author": {"name": "王小明", "city": "北京"}, "tags": ["硬件", "中文"]},
    {"id": "p2", "title": "显示器选购指南", "price": 1299,
     "author": {"name": "李华", "city": "上海"}},
    {"id": "p3", "title": "人体工学椅，横评（含逗号）", "price": 1599,
     "author": {"name": "张三", "city": "广州"}, "note": "含英文逗号, 与\n换行"},
    {"id": "p4", "title": "降噪耳机体验", "price": 899,
     "author": {"name": "王小明", "city": "北京"}},
    {"id": "p5", "title": "快充充电器对比",
     "author": {"name": "陈静", "city": "深圳"}, "price": 99},
    {"id": "p6", "title": "移动电源推荐", "price": 199,
     "author": {"name": "李华", "city": "上海"}},
    {"id": "p7", "title": "网络摄像头盘点", "price": 259,
     "author": {"name": "赵雷"}},  # author.city 故意缺失，演示留空
    {"id": "p8", "title": "数据线避坑", "price": 29,
     "author": {"name": "陈静", "city": "深圳"}},
]

SCENARIOS = ["normal", "loop", "ratelimit", "error", "redirect", "offsite_next"]
PAGE_SIZE = 3

# loop 场景的固定翻页链：1 -> 2 -> 3 -> 2（绕回），其中 l2 重复且内容变化
LOOP_PAGES = {
    1: {"items": [
        {"id": "l1", "title": "循环页-商品甲", "author": {"name": "周一"}},
        {"id": "l2", "title": "循环页-商品乙（第一次）", "author": {"name": "周二"}},
        {"id": "l3", "title": "循环页-商品丙", "author": {"name": "周三"}},
    ], "next": "/api/items?page=2"},
    2: {"items": [
        {"id": "l4", "title": "循环页-商品丁", "author": {"name": "周四"}},
        {"id": "l5", "title": "循环页-商品戊", "author": {"name": "周五"}},
    ], "next": "/api/items?page=3"},
    3: {"items": [
        {"id": "l2", "title": "循环页-商品乙（第二次，内容已变）",
         "author": {"name": "周二"}},
        {"id": "l6", "title": "循环页-商品己", "author": {"name": "周六"}},
    ], "next": "/api/items?page=2"},  # 绕回第 2 页
}


class TestSiteState:
    def __init__(self, state_path: str, external_base: str):
        self._lock = threading.RLock()
        self._path = state_path
        self.external_base = external_base
        self.records = [dict(r) for r in SEED_RECORDS]
        self.scenario = "normal"
        self.request_hits = 0  # ratelimit 场景计数
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.records = data.get("records", self.records)
            self.scenario = data.get("scenario", "normal")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def save(self) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({"records": self.records, "scenario": self.scenario},
                      f, ensure_ascii=False, indent=2)

    def lock(self):
        return self._lock

    def page_items(self, page: int) -> dict:
        scenario = self.scenario
        if scenario == "loop":
            return LOOP_PAGES.get(page, {"items": [], "next": None})
        records = self.records
        start = (page - 1) * PAGE_SIZE
        items = records[start:start + PAGE_SIZE]
        if start + PAGE_SIZE < len(records):
            next_url = f"/api/items?page={page + 1}"
        else:
            next_url = None
        if scenario == "offsite_next" and page == 1:
            next_url = self.external_base + "/external"
        return {"items": items, "next": next_url}


def make_test_handler(state: TestSiteState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 安静一点
            pass

        def _send(self, code: int, payload, headers=None) -> None:
            if isinstance(payload, (dict, list)):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            else:
                body = payload
                ctype = "text/plain; charset=utf-8"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_OPTIONS(self):
            self._send(204, b"")

        # ---------- GET ----------
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            with state.lock():
                if path == "/api/config":
                    return self._send(200, {
                        "scenarios": SCENARIOS, "scenario": state.scenario,
                        "record_count": len(state.records),
                        "page_size": PAGE_SIZE,
                        "external_base": state.external_base})
                if path == "/api/records":
                    return self._send(200, state.records)
                if path == "/api/items":
                    return self._handle_items(parse_qs(parsed.query))
            return self._send(404, {"error": "not found"})

        def _handle_items(self, query) -> None:
            state.request_hits += 1
            scenario = state.scenario
            if scenario == "error":
                return self._send(500, {"error": "网站内部错误（演示）"})
            if scenario == "ratelimit" and state.request_hits <= 2:
                # 网站明确给出允许重试的时间
                return self._send(429, {"error": "请求过于频繁（演示）"},
                                  {"Retry-After": "1"})
            if scenario == "redirect":
                return self._send(302, b"",
                                  {"Location": state.external_base + "/external"})
            page = int(query.get("page", ["1"])[0])
            return self._send(200, state.page_items(page))

        # ---------- POST/PUT/DELETE ----------
        def do_POST(self):
            path = urlparse(self.path).path
            with state.lock():
                if path == "/api/control":
                    body = self._read_json()
                    scenario = body.get("scenario")
                    if scenario not in SCENARIOS:
                        return self._send(400, {"error": f"未知场景：{scenario}"})
                    state.scenario = scenario
                    state.request_hits = 0
                    state.save()
                    return self._send(200, {"ok": True, "scenario": scenario})
                if path == "/api/records":
                    body = self._read_json()
                    records = body.get("records")
                    if not isinstance(records, list):
                        return self._send(400, {"error": "需要 records 数组"})
                    state.records = records
                    state.save()
                    return self._send(200, {"ok": True, "count": len(records)})
                if path == "/api/records/reset":
                    state.records = [dict(r) for r in SEED_RECORDS]
                    state.save()
                    return self._send(200, {"ok": True, "count": len(state.records)})
                if path == "/api/records/add":
                    record = self._read_json()
                    if "id" not in record:
                        return self._send(400, {"error": "记录必须包含 id"})
                    state.records = [r for r in state.records if r["id"] != record["id"]]
                    state.records.append(record)
                    state.save()
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})

        def do_PUT(self):
            path = urlparse(self.path).path
            with state.lock():
                if path.startswith("/api/records/"):
                    rid = path.rsplit("/", 1)[-1]
                    record = self._read_json()
                    record["id"] = rid
                    state.records = [r for r in state.records if r["id"] != rid]
                    state.records.append(record)
                    state.save()
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})

        def do_DELETE(self):
            path = urlparse(self.path).path
            with state.lock():
                if path.startswith("/api/records/"):
                    rid = path.rsplit("/", 1)[-1]
                    state.records = [r for r in state.records if r["id"] != rid]
                    state.save()
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})

    return Handler


class _ExternalHandler(BaseHTTPRequestHandler):
    """模拟“别的网站”：若采集器错误跟随跳转，就会收到这份外站数据。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({
            "items": [{"id": "EVIL-1", "title": "外站数据，不应被采集",
                       "author": {"name": "外部网站"}}],
            "next": None,
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_test_site(state_path: str, host: str = "127.0.0.1", port: int = 0,
                    external_port: int = 0) -> tuple[ThreadingHTTPServer,
                                                      ThreadingHTTPServer,
                                                      TestSiteState]:
    external = ThreadingHTTPServer((host, external_port), _ExternalHandler)
    external_base = f"http://{host}:{external.server_address[1]}"
    state = TestSiteState(state_path, external_base)
    server = ThreadingHTTPServer((host, port), make_test_handler(state))
    t1 = threading.Thread(target=server.serve_forever, daemon=True)
    t2 = threading.Thread(target=external.serve_forever, daemon=True)
    t1.start()
    t2.start()
    return server, external, state
