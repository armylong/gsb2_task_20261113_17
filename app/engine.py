"""采集引擎：线程池调度、暂停/恢复、重启续采、翻页绕回保护、轮次生命周期。"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Optional

from .db import Database
from .fetcher import PermanentFetchError, fetch_page
from .utils import HostRateLimiter, normalize_origin, resolve_url, same_origin

log = logging.getLogger("collector.engine")

_SENTINEL = None


class _RoundEvents:
    """一轮采集的暂停/停止信号，重启后重建。"""

    def __init__(self, paused: bool = False):
        self.pause = threading.Event()
        self.stop = threading.Event()
        self.closing = threading.Event()  # 程序关停：保留进度，不算失败
        if paused:
            self.pause.set()

    def wait_runnable(self) -> bool:
        """暂停时阻塞，停止时返回 False。"""
        while self.pause.is_set():
            if self.stop.wait(timeout=0.2):
                return False
        return not self.stop.is_set()


class Engine:
    def __init__(self, db: Database, rate_limiter: HostRateLimiter):
        self.db = db
        self.limiter = rate_limiter
        self._lock = threading.RLock()
        self._active: dict[int, _RoundEvents] = {}
        self._threads: list[threading.Thread] = []

    # ---------- 生命周期 ----------
    def start(self) -> None:
        """程序启动时恢复所有未结束的轮次（暂停中的轮次仍保持暂停）。"""
        rows = self.db.query_all(
            "SELECT * FROM rounds WHERE status IN ('queued','running','paused')")
        for row in rows:
            task = self.db.get_task(row["task_id"])
            if task is None:
                continue
            paused = row["status"] == "paused"
            self._spawn_round(row["id"], task, paused)
            if paused:
                self.db.update_task_status(task["id"], "paused")
            else:
                self.db.update_round(row["id"], status="running")
                self.db.update_task_status(task["id"], "running")

    def shutdown(self, timeout: float = 10.0) -> None:
        """通知所有工作线程退出并等待；数据库中的进度保持原样，下次启动续采。"""
        with self._lock:
            events = list(self._active.values())
            threads = list(self._threads)
        for ev in events:
            ev.closing.set()
            ev.stop.set()
        for t in threads:
            t.join(timeout=timeout)

    # ---------- 轮次控制 ----------
    def start_round(self, task_id: int) -> int:
        task = self.db.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        with self._lock:
            for r in self.db.list_rounds(task_id):
                if r["status"] in ("queued", "running", "paused") and r["id"] in self._active:
                    raise RuntimeError("该任务已有正在进行的轮次，请先结束或等待其完成")
            round_id = self.db.create_round(task_id, task["entry_url"])
            self.limiter.configure(task["origin"], task["min_interval"])
            self.db.update_round(round_id, status="running")
            self.db.update_task_status(task_id, "running")
            self._spawn_round(round_id, task, paused=False)
        return round_id

    def pause_round(self, round_id: int) -> None:
        with self._lock:
            ev = self._active.get(round_id)
            row = self.db.get_round(round_id)
            if ev is None or row is None:
                raise RuntimeError("该轮次不在运行中")
            ev.pause.set()
            self.db.update_round(round_id, status="paused")
            self.db.update_task_status(row["task_id"], "paused")

    def resume_round(self, round_id: int) -> None:
        with self._lock:
            ev = self._active.get(round_id)
            row = self.db.get_round(round_id)
            if ev is None or row is None:
                raise RuntimeError("该轮次不在运行中")
            ev.pause.clear()
            self.db.update_round(round_id, status="running")
            self.db.update_task_status(row["task_id"], "running")

    def is_active(self, round_id: int) -> bool:
        return round_id in self._active

    # ---------- 工作线程 ----------
    def _spawn_round(self, round_id: int, task, paused: bool) -> None:
        ev = _RoundEvents(paused=paused)
        with self._lock:
            self._active[round_id] = ev
            coordinator = threading.Thread(
                target=self._run_round, args=(round_id, dict(task), ev),
                name=f"round-{round_id}", daemon=True)
            self._threads.append(coordinator)
        coordinator.start()

    def _run_round(self, round_id: int, task: dict, ev: _RoundEvents) -> None:
        concurrency = max(1, int(task["max_concurrency"]))
        sem = threading.BoundedSemaphore(concurrency)
        q: queue.Queue = queue.Queue()
        state = {"failed": False, "error": "", "status_code": None}

        first = self.db.get_round(round_id)
        q.put(first["pending_url"] or task["entry_url"])

        def worker() -> None:
            while True:
                url = q.get()
                try:
                    if url is _SENTINEL:
                        return
                    if not ev.wait_runnable():
                        return
                    sem.acquire()
                    try:
                        self._process_url(round_id, task, url, q, ev, state)
                    finally:
                        sem.release()
                except PermanentFetchError as e:
                    if ev.closing.is_set():
                        # 程序正常关停：保留数据库中的进度与状态，重启后续采
                        self._drain_queue(q)
                        return
                    # 致命错误：记录失败请求，结束整轮
                    state["failed"] = True
                    state["error"] = str(e)
                    state["status_code"] = e.status_code
                    self.db.add_failure(round_id, url, e.status_code,
                                        getattr(e, "attempts", 1), str(e))
                    ev.stop.set()
                    self._drain_queue(q)
                except Exception as e:  # 防御性兜底，避免线程静默死掉
                    if ev.closing.is_set():
                        self._drain_queue(q)
                        return
                    log.exception("round %s unexpected error", round_id)
                    state["failed"] = True
                    state["error"] = f"内部错误：{e}"
                    ev.stop.set()
                    self._drain_queue(q)
                finally:
                    q.task_done()

        workers = [threading.Thread(target=worker, name=f"round-{round_id}-w{i}",
                                    daemon=True) for i in range(concurrency)]
        for w in workers:
            w.start()
        q.join()
        for _ in workers:
            q.put(_SENTINEL)
        for w in workers:
            w.join(timeout=5)

        # 单一收尾线程，状态转换无竞争
        row = self.db.get_round(round_id)
        if row["status"] in ("completed", "failed"):
            with self._lock:
                self._active.pop(round_id, None)
            return
        if ev.closing.is_set():
            # 程序关停：保持轮次与任务原状态（含暂停），进度已在数据库，重启续采
            with self._lock:
                self._active.pop(round_id, None)
            return
        count = self.db.recount_round_records(round_id)
        if state["failed"]:
            self.db.update_round(round_id, status="failed", error=state["error"],
                                 finished_at=__import__("time").time())
            self.db.update_task_status(task["id"], "failed")
        else:
            self.db.update_round(round_id, status="completed", pending_url=None,
                                 record_count=count,
                                 finished_at=__import__("time").time())
            self.db.update_task_status(task["id"], "completed")
        with self._lock:
            self._active.pop(round_id, None)

    @staticmethod
    def _drain_queue(q: queue.Queue) -> None:
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                return
            if item is _SENTINEL:
                q.put(_SENTINEL)
            else:
                q.task_done()

    def _process_url(self, round_id: int, task: dict, url: str,
                     q: queue.Queue, ev: _RoundEvents, state: dict) -> None:
        # 外站地址（理论上入口已校验，这里再防下一页解析异常）
        if not same_origin(url, task["origin"]):
            raise PermanentFetchError(
                f"下一页指向了别的网站或端口，已停止采集：{url}")

        # 同网站共享限速；暂停/停止可打断等待
        if not self.limiter.acquire(task["origin"], ev.stop, ev.pause):
            return

        def on_retry() -> None:
            self.db.bump_round_stats(round_id, retries=1)

        data, final_url = fetch_page(
            url, task["origin"], int(task["max_retries"]),
            ev.stop, ev.pause, on_retry=on_retry)

        # 即使该地址重启后被重新采到（上次崩溃在写入下一页地址之前），
        # 也要正常解析它的 next，否则会丢后续页面；记录按 id upsert 天然幂等。
        already_seen = not self.db.mark_visited(round_id, final_url)

        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise PermanentFetchError("响应结构不符合约定：缺少列表字段 items")
        for item in items:
            if not isinstance(item, dict) or "id" not in item:
                raise PermanentFetchError("记录缺少稳定编号 id，无法保证去重")
            # 同一轮重复 id：覆盖为最后一次成功采到的内容
            self.db.upsert_record(round_id, item["id"], item)
        if not already_seen:
            # 页数只统计不同页面；重启重采同一页不重复计数
            self.db.bump_round_stats(round_id, pages=1)
            self.db.recount_round_records(round_id)

        next_field = data.get("next")
        if next_field in (None, ""):
            # null 表示采完
            self.db.update_round(round_id, pending_url=None)
            return

        next_url = resolve_url(final_url, str(next_field))
        if not same_origin(next_url, task["origin"]):
            raise PermanentFetchError(
                f"下一页指向了别的网站或端口（协议/域名/端口不一致），已停止采集：{next_url}")
        if self.db.is_visited(round_id, next_url):
            # 下一页绕回已采页面：停止，避免无限采集
            self.db.update_round(round_id, pending_url=None)
            return
        # 持久化下一个地址：任何时刻崩溃/重启都能从这里继续，不重开
        self.db.update_round(round_id, pending_url=next_url)
        q.put(next_url)
