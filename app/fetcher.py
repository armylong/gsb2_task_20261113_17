"""HTTP 抓取层：同源校验、跳转拦截、限流/临时错误重试、致命错误分类。"""
from __future__ import annotations

import email.utils
import json
import socket
import threading
import urllib.error
import urllib.request
from typing import Any, Optional

from .utils import normalize_origin, same_origin

USER_AGENT = "LocalCollector/1.0 (+local research tool)"
DEFAULT_TIMEOUT = 15.0


class FetchError(Exception):
    """抓取失败基类。"""

    def __init__(self, message: str, status_code: Optional[int] = None,
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.attempts = 1


class PermanentFetchError(FetchError):
    """地址不存在、无权限、外站跳转等：不应重试，整轮停止。"""


class TransientFetchError(FetchError):
    """超时、临时 5xx、429 等：可以重试。"""


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """只允许同源跳转；跳转到外站（协议/域名/端口不一致）直接报错。"""

    def __init__(self, origin: str):
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not same_origin(newurl, self.origin):
            raise PermanentFetchError(
                f"页面跳转到了外站，已停止采集：{newurl}", status_code=code)
        new_req = super().redirect_request(
            req, fp, code, msg, headers, newurl)
        # 同源跳转继续限制在本网站内
        return new_req


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    # 优先按秒数解析
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    # 否则按 HTTP 日期解析
    date = email.utils.parsedate_to_datetime(value)
    if date is not None:
        return max(0.0, date.timestamp() - __import__("time").time())
    return None


def _http_request(url: str, origin: str, timeout: float) -> tuple[bytes, str, int]:
    """执行单次请求，返回 (响应体, 最终地址, 状态码)。同源由 opener 保证。"""
    opener = urllib.request.build_opener(_SameOriginRedirectHandler(origin))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        retry_after = _parse_retry_after(e.headers.get("Retry-After"))
        if e.code == 429:
            raise TransientFetchError(
                f"网站提示请求过于频繁（429），需等待后重试",
                status_code=429, retry_after=retry_after)
        if 500 <= e.code < 600:
            raise TransientFetchError(
                f"网站临时出错（HTTP {e.code}）",
                status_code=e.code, retry_after=retry_after)
        # 404、401、403 等其他状态码：致命错误，停止整轮
        raise PermanentFetchError(
            f"请求被拒绝或地址不存在（HTTP {e.code}）", status_code=e.code)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise TransientFetchError(f"请求超时：{reason}")
        # 连接被拒绝、DNS 失败等网络问题按临时错误处理（有重试上限）
        raise TransientFetchError(f"网络连接失败：{reason}")
    except PermanentFetchError:
        raise
    except Exception as e:  # 协议级异常等
        raise TransientFetchError(f"请求异常：{e}")

    with resp:
        body = resp.read()
        final_url = resp.geturl()
        status = getattr(resp, "status", 200)
    if not same_origin(final_url, origin):
        raise PermanentFetchError(
            f"最终地址属于外站，已停止采集：{final_url}", status_code=status)
    return body, final_url, status


def fetch_page(url: str, origin: str, max_retries: int,
               stop_event: threading.Event,
               pause_event: threading.Event,
               on_retry=None,
               timeout: float = DEFAULT_TIMEOUT) -> tuple[dict[str, Any], str]:
    """抓取一个 JSON 列表页，带次数受限的重试。

    成功返回 (解析后的 JSON, 最终实际访问地址)。
    重试耗尽或致命错误抛出对应 FetchError（异常上带 attempts 尝试次数）。
    """
    attempt = 0
    while True:
        # 每次尝试（含重试）前都检查暂停/停止；暂停时阻塞在这里
        if not _wait_runnable(stop_event, pause_event):
            raise PermanentFetchError("任务已停止")
        attempt += 1
        try:
            body, final_url, _status = _http_request(url, origin, timeout)
            try:
                return json.loads(body.decode("utf-8")), final_url
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                # 返回内容不是合法 JSON，属于地址/数据问题，不重试
                raise PermanentFetchError(f"响应不是合法的 JSON 数据：{e}")
        except PermanentFetchError as e:
            e.attempts = attempt
            raise
        except TransientFetchError as e:
            e.attempts = attempt
            if attempt > max_retries:
                wrapped = PermanentFetchError(
                    f"重试 {max_retries} 次后仍失败：{e}",
                    status_code=e.status_code)
                wrapped.attempts = attempt  # 实际发出的请求次数
                raise wrapped from e
            if on_retry is not None:
                on_retry()
            # 429 优先使用网站给出的 Retry-After，否则指数退避（封顶 10 秒）
            if e.retry_after is not None:
                delay = e.retry_after
            else:
                delay = min(10.0, 0.5 * (2 ** (attempt - 1)))
            if _interruptible_sleep(delay, stop_event, pause_event):
                raise PermanentFetchError("任务已停止")


def _wait_runnable(stop_event: threading.Event,
                   pause_event: threading.Event) -> bool:
    """暂停时阻塞（分段等待），停止时返回 False；可运行时返回 True。"""
    while pause_event.is_set():
        if stop_event.wait(timeout=0.2):
            return False
    return not stop_event.is_set()


def _interruptible_sleep(seconds: float, stop_event: threading.Event,
                         pause_event: threading.Event) -> bool:
    """可被暂停/停止打断的睡眠。返回 True 表示被停止。

    暂停期间不计入退避时间（恢复后继续等剩余时间）。
    """
    import time as _time
    end = _time.monotonic() + seconds
    while True:
        if stop_event.is_set():
            return True
        remaining = end - _time.monotonic()
        if remaining <= 0:
            return False
        if pause_event.is_set():
            stop_event.wait(timeout=0.2)
            continue
        if stop_event.wait(timeout=min(0.2, remaining)):
            return True
