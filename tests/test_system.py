"""端到端自动测试：用真实 HTTP 服务验证采集器的关键保证。

运行：
    python3 -m unittest discover -s tests -v
或：
    python3 tests/test_system.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request
import warnings

warnings.simplefilter("ignore", ResourceWarning)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.testsite import start_test_site, SEED_RECORDS
from app.web import App

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIELDS = [
    {"path": "id", "column": "编号"},
    {"path": "title", "column": "标题"},
    {"path": "price", "column": "价格"},
    {"path": "author.name", "column": "作者姓名"},
    {"path": "author.city", "column": "作者城市"},
    {"path": "note", "column": "备注"},
]


class CollectorTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.ts, self.ext, self.state = start_test_site(
            os.path.join(self.tmp, "ts.json"), port=0, external_port=0)
        self.test_base = f"http://127.0.0.1:{self.ts.server_address[1]}"
        self.db_path = os.path.join(self.tmp, "collector.db")
        self.app = App(self.db_path, os.path.join(ROOT, "web"))
        self.app.test_site_base = self.test_base
        self.base_url = self.app.start(port=0)

    def tearDown(self) -> None:
        self.app.stop()
        self.ts.shutdown()
        self.ts.server_close()
        self.ext.shutdown()
        self.ext.server_close()

    # ---------- 辅助 ----------
    def api(self, path: str, body=None, method="GET"):
        if body is None:
            with urllib.request.urlopen(self.base_url + path, timeout=10) as r:
                return json.load(r)
        req = urllib.request.Request(
            self.base_url + path,
            json.dumps(body).encode("utf-8"),
            {"Content-Type": "application/json"}, method=method)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def call_api(self, path: str, method="POST", body=None):
        return self.api(path, body=body if body is not None else {}, method=method)

    def create_task(self, name="任务", min_interval=0.05, concurrency=2,
                    max_retries=3, entry_url=None):
        return self.api("/api/tasks", {
            "name": name,
            "entry_url": entry_url or self.test_base + "/api/items",
            "max_concurrency": concurrency,
            "min_interval": min_interval,
            "max_retries": max_retries,
            "fields": FIELDS,
        }, method="POST")

    def start_round(self, task_id: int):
        return self.call_api(f"/api/tasks/{task_id}/rounds")

    def wait_round(self, round_id: int, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.api(f"/api/rounds/{round_id}")
            if r["status"] in ("completed", "failed"):
                return r
            time.sleep(0.1)
        self.fail(f"轮次 {round_id} 未在 {timeout}s 内结束")

    def wait_status(self, round_id: int, status: str, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.api(f"/api/rounds/{round_id}")
            if r["status"] == status:
                return r
            time.sleep(0.05)
        self.fail(f"轮次未进入 {status} 状态，当前：{r['status']}")

    def set_scenario(self, scenario: str):
        req = urllib.request.Request(
            self.test_base + "/api/control",
            json.dumps({"scenario": scenario}).encode(),
            {"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()

    def set_records(self, records: list):
        req = urllib.request.Request(
            self.test_base + "/api/records",
            json.dumps({"records": records}).encode(),
            {"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()

    def round_records(self, round_id: int) -> list[dict]:
        data = self.api(f"/api/rounds/{round_id}/records?page=1&size=200")
        return data["records"]

    def export(self, round_id: int, fmt: str) -> bytes:
        with urllib.request.urlopen(
                self.base_url + f"/api/rounds/{round_id}/export?format={fmt}",
                timeout=10) as r:
            return r.read()

    def seed_large_dataset(self, count: int = 60):
        """灌入 count 条记录（每页 3 条 → count/3 页），给暂停/重启留出充足窗口。"""
        records = [{"id": f"x{i}", "title": f"记录{i}",
                    "author": {"name": f"作者{i}"}} for i in range(count)]
        self.set_records(records)
        return records

    def schedule_pause(self, round_id: int, delay: float = 0.7):
        """用独立线程在 delay 秒后发暂停，避免轮询延迟导致小数据集提前采完。"""
        import threading

        def _pause():
            time.sleep(delay)
            try:
                self.call_api(f"/api/rounds/{round_id}/pause")
            except Exception:
                pass

        threading.Thread(target=_pause, daemon=True).start()


class TestRestartResume(CollectorTestBase):
    """程序重启后必须接着采，不能从头另开一轮。"""

    def test_restart_resumes_unfinished_round(self):
        self.seed_large_dataset(60)  # 20 页
        task = self.create_task(min_interval=0.15)
        round_row = self.start_round(task["id"])
        rid = round_row["id"]
        # 定时在采集中途暂停（暂停状态会持久化在数据库）
        self.schedule_pause(rid, delay=0.7)
        paused = self.wait_status(rid, "paused")
        pages_before = paused["pages_fetched"]
        self.assertGreaterEqual(pages_before, 1)
        self.assertLess(pages_before, 20)

        # 模拟程序重启：停掉整个 App，再用同一个数据库文件新建 App
        self.app.stop()
        self.app = App(self.db_path, os.path.join(ROOT, "web"))
        self.app.test_site_base = self.test_base
        self.app.start(port=0)
        # 端口可能变化，更新基址
        host, port = self.app.server.server_address
        self.base_url = f"http://{host}:{port}"

        # 重启后轮次仍是“已暂停”，且页数进度保留
        restarted = self.api(f"/api/rounds/{rid}")
        self.assertEqual(restarted["status"], "paused")
        self.assertEqual(restarted["pages_fetched"], pages_before)
        self.assertEqual(len(self.app.engine._active), 1,
                         "重启后应自动恢复未结束的轮次（保持暂停）")

        # 继续后应在同一轮内跑完，而不是新开一轮
        self.call_api(f"/api/rounds/{rid}/resume")
        finished = self.wait_round(rid)
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["pages_fetched"], 20)  # 60 条 / 每页 3 条
        self.assertEqual(finished["record_count"], 60)
        rounds = self.api(f"/api/tasks/{task['id']}")["rounds"]
        self.assertEqual(len(rounds), 1, "重启续采不能产生新轮次")


class TestSharedRateLimit(CollectorTestBase):
    """多个任务采同一网站时，必须共同遵守该网站的访问间隔。"""

    def test_two_tasks_share_host_interval(self):
        interval = 0.5
        t1 = self.create_task("任务A", min_interval=interval, concurrency=1)
        t2 = self.create_task("任务B", min_interval=interval, concurrency=1)
        start = time.monotonic()
        r1 = self.start_round(t1["id"])
        r2 = self.start_round(t2["id"])
        self.wait_round(r1["id"])
        self.wait_round(r2["id"])
        elapsed = time.monotonic() - start

        # 两个任务各采 3 页，共 6 次请求；共享限速下任意相邻两次请求
        # 至少间隔 interval，因此总耗时至少 (6-1)*interval
        self.assertGreaterEqual(
            elapsed, 5 * interval - 0.15,
            f"两任务共用同站限速未生效：6 次请求仅耗时 {elapsed:.2f}s")

        # 两个轮次都完整采齐
        for rid in (r1["id"], r2["id"]):
            r = self.api(f"/api/rounds/{rid}")
            self.assertEqual(r["status"], "completed")
            self.assertEqual(r["record_count"], len(SEED_RECORDS))


class TestPauseResume(CollectorTestBase):
    """暂停后不发新请求；继续后不漏页、不重复保存记录。"""

    def test_pause_then_resume_no_missing_no_duplicate(self):
        records = self.seed_large_dataset(60)  # 20 页，暂停窗口充足
        task = self.create_task(min_interval=0.15)
        rid = self.start_round(task["id"])["id"]
        self.schedule_pause(rid, delay=0.7)
        self.wait_status(rid, "paused")
        pages_when_paused = self.api(f"/api/rounds/{rid}")["pages_fetched"]
        self.assertLess(pages_when_paused, 20, "暂停必须在采集中途生效")
        time.sleep(0.8)
        # 暂停期间页数与记录数都不能再增长
        self.assertEqual(self.api(f"/api/rounds/{rid}")["pages_fetched"],
                         pages_when_paused)

        self.call_api(f"/api/rounds/{rid}/resume")
        finished = self.wait_round(rid)
        self.assertEqual(finished["status"], "completed")
        got = self.round_records(rid)
        ids = [r["record_id"] for r in got]
        self.assertEqual(len(ids), len(set(ids)), "记录 id 出现重复保存")
        self.assertEqual(set(ids), {r["id"] for r in records},
                         "暂停继续后漏采了记录")
        self.assertEqual(finished["pages_fetched"], 20)
        self.assertEqual(finished["record_count"], 60)


class TestFailedRoundNoDeletion(CollectorTestBase):
    """失败/未完成的轮次不能把暂时没采到的记录误判为“消失”。"""

    def test_failed_round_reports_no_removed(self):
        # 第一轮先完整采齐
        task = self.create_task(max_retries=1)
        r1 = self.start_round(task["id"])
        self.assertEqual(self.wait_round(r1["id"])["status"], "completed")
        first_ids = {r["record_id"] for r in self.round_records(r1["id"])}

        # 切换到持续 500，第二轮必定失败
        self.set_scenario("error")
        r2 = self.start_round(task["id"])
        failed = self.wait_round(r2["id"])
        self.assertEqual(failed["status"], "failed")
        failures = self.api(f"/api/rounds/{r2['id']}/failures")
        self.assertTrue(failures, "失败轮次必须留下失败请求记录")
        self.assertEqual(failures[0]["status_code"], 500)
        self.assertTrue(failures[0]["reason"])

        # 失败轮次不允许做对比（无法判断消失）
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.api(f"/api/rounds/{r2['id']}/diff")
        self.assertEqual(ctx.exception.code, 400)

        # 上一轮完整结果仍可查看，数据未受影响
        old = self.round_records(r1["id"])
        self.assertEqual({r["record_id"] for r in old}, first_ids)


class TestExportImmutability(CollectorTestBase):
    """旧轮次导出的内容不受后续轮次更新影响，且对比结果与实际修改一致。"""

    def test_export_frozen_and_diff_matches_changes(self):
        task = self.create_task()
        r1 = self.start_round(task["id"])
        self.assertEqual(self.wait_round(r1["id"])["status"], "completed")
        csv1 = self.export(r1["id"], "csv")
        json1 = self.export(r1["id"], "json")

        # 修改数据：新增 1 条、修改 1 条、删除 1 条
        records = [dict(r) for r in SEED_RECORDS]
        records[0]["title"] = "机械键盘评测（已更新标题）"
        records[0]["author"] = dict(records[0]["author"], name="王大名")
        removed = records.pop(1)  # 删除 p2
        records.append({"id": "p9", "title": "全新商品（中文）", "price": 66,
                        "author": {"name": "新作者", "city": "杭州"}})
        self.set_records(records)

        r2 = self.start_round(task["id"])
        self.assertEqual(self.wait_round(r2["id"])["status"], "completed")

        # 第一轮导出必须保持原样
        self.assertEqual(self.export(r1["id"], "csv"), csv1)
        self.assertEqual(self.export(r1["id"], "json"), json1)

        # 第二轮对比：新增/变化/消失各 1 条，且编号与实际修改对得上
        diff = self.api(f"/api/rounds/{r2['id']}/diff")
        self.assertEqual(diff["summary"],
                         {"added": 1, "changed": 1, "removed": 1, "unchanged": len(SEED_RECORDS) - 2})
        self.assertEqual(diff["added"], ["p9"])
        self.assertEqual(diff["changed"], ["p1"])
        self.assertEqual(diff["removed"], [removed["id"]])

        # CSV 中的中文、逗号、换行正常保存（utf-8-sig + RFC4180 转义）
        text = self.export(r2["id"], "csv").decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0], ["编号", "标题", "价格", "作者姓名", "作者城市", "备注"])
        self.assertIn("全新商品（中文）", text)
        p3 = next(r for r in rows if r[0] == "p3")
        self.assertIn("，", p3[1])          # 中文逗号正常保存
        self.assertIn(",", p3[5])           # 英文逗号正常保存（RFC4180 按需加引号）
        self.assertIn("\n", p3[5])          # 换行被引号保护，仍在同一单元格

        # JSON 导出为完整原始快照
        exported_json = json.loads(self.export(r2["id"], "json"))
        self.assertEqual({r["id"] for r in exported_json},
                         {r["id"] for r in records})


class TestScenarioGuards(CollectorTestBase):
    """场景回归：绕回保护、429 遵守 Retry-After、外站拦截。"""

    def test_loop_scenario_stops_and_keeps_last_seen(self):
        self.set_scenario("loop")
        task = self.create_task(min_interval=0.02, max_retries=1)
        rid = self.start_round(task["id"])["id"]
        finished = self.wait_round(rid)
        self.assertEqual(finished["status"], "completed")
        records = {r["record_id"]: r["raw"] for r in self.round_records(rid)}
        # l1..l6 共 6 个编号；l2 重复出现，保留最后一次采到的内容
        self.assertEqual(set(records), {f"l{i}" for i in range(1, 7)})
        self.assertIn("第二次", records["l2"]["title"])
        # 只访问了 3 个不同页面（绕回的第 2 页不会再次访问）
        self.assertEqual(finished["pages_fetched"], 3)

    def test_rate_limit_recovers_with_retry_after(self):
        self.set_scenario("ratelimit")
        task = self.create_task(min_interval=0.02, max_retries=4)
        rid = self.start_round(task["id"])["id"]
        finished = self.wait_round(rid)
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["record_count"], len(SEED_RECORDS))
        self.assertGreaterEqual(finished["retry_count"], 2,
                                "前两次 429 应计入重试")

    def test_redirect_to_external_site_blocked(self):
        self.set_scenario("redirect")
        task = self.create_task(max_retries=1)
        rid = self.start_round(task["id"])["id"]
        finished = self.wait_round(rid)
        self.assertEqual(finished["status"], "failed")
        failures = self.api(f"/api/rounds/{rid}/failures")
        self.assertEqual(failures[0]["status_code"], 302)
        self.assertIn("外站", failures[0]["reason"])
        # 外站数据一条都不能进来
        self.assertEqual(self.round_records(rid), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
