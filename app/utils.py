"""通用工具：同源判断、嵌套字段取值、按网站共享的限速器。"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit, urljoin

# 默认端口归一化，保证 https://x.com 与 https://x.com:443 视为同一网站
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_origin(url: str) -> str:
    """返回 协议://主机:端口（默认端口省略）。"""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"网址无效（仅支持 http/https）：{url}")
    host = parts.hostname.lower()
    port = parts.port
    if port is None or port == _DEFAULT_PORTS.get(parts.scheme):
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def same_origin(url: str, origin: str) -> bool:
    try:
        return normalize_origin(url) == origin
    except ValueError:
        return False


def resolve_url(base_url: str, maybe_relative: str) -> str:
    """下一页只给路径时，依据当前实际访问地址解析为完整地址。"""
    return urljoin(base_url, maybe_relative)


def get_nested(data, dotted_path: str):
    """按 'author.name' 这样的路径取嵌套字段，任一层缺失返回 None。"""
    cur = data
    for part in dotted_path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


class HostRateLimiter:
    """全进程共享的按网站限速器。

    多个任务采集同一网站时共用一个实例，保证该网站任意两次请求
    开始发出的时间间隔不小于 min_interval（取所有任务中最严格的设置）。
    """

    def __init__(self) -> None:
        # 用 RLock：采集流程可能在持有数据库 RLock 的同一线程内进入限速器
        self._cond = threading.Condition(threading.RLock())
        self._last_request: dict[str, float] = {}
        self._intervals: dict[str, float] = {}

    def configure(self, origin: str, min_interval: float) -> None:
        with self._cond:
            old = self._intervals.get(origin, 0.0)
            if min_interval > old:
                self._intervals[origin] = float(min_interval)
            self._cond.notify_all()

    def acquire(self, origin: str, stop_event: threading.Event | None = None,
                pause_event: threading.Event | None = None) -> bool:
        """阻塞直到该网站允许发下一次请求。返回 False 表示等待期间被停止。"""
        with self._cond:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return False
                paused = pause_event is not None and pause_event.is_set()
                interval = 0.0 if paused else self._intervals.get(origin, 0.0)
                now = time.monotonic()
                earliest = self._last_request.get(origin, 0.0) + interval
                wait = earliest - now
                if wait <= 0:
                    self._last_request[origin] = now
                    return True
                # 分段等待，暂停/停止能及时打断
                if self._cond.wait(timeout=min(wait, 0.2)):
                    pass
