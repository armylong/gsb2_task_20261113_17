#!/usr/bin/env python3
"""一条命令同时启动采集工具和内置测试网站。

用法：
    python3 run.py                # 默认端口：工具 8000，测试网站 8011
    python3 run.py --port 9000 --test-port 9011

然后浏览器打开启动后打印的“采集工具地址”。
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.testsite import start_test_site
from app.web import App

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    parser = argparse.ArgumentParser(description="本地网页采集工具")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="采集工具端口")
    parser.add_argument("--test-port", type=int, default=8011,
                        help="内置测试网站端口")
    parser.add_argument("--data-dir", default=os.path.join(BASE_DIR, "data"))
    args = parser.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)
    db_path = os.path.join(args.data_dir, "collector.db")
    state_path = os.path.join(args.data_dir, "testsite.json")
    web_dir = os.path.join(BASE_DIR, "web")

    test_server, external_server, _state = start_test_site(
        state_path, host=args.host, port=args.test_port)
    test_base = f"http://{args.host}:{test_server.server_address[1]}"

    application = App(db_path, web_dir)
    application.test_site_base = test_base
    base_url = application.start(host=args.host, port=args.port)

    print("=" * 60)
    print("采集工具已启动：")
    print(f"  采集工具页面 : {base_url}")
    print(f"  内置测试网站 : {test_base}/api/items")
    print("按 Ctrl+C 停止（进度已保存在 SQLite，重启后自动续采）")
    print("=" * 60)

    stop = {"flag": False}

    def _handle_signal(_signum, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        while not stop["flag"]:
            time.sleep(0.3)
    finally:
        application.stop()
        test_server.shutdown()
        external_server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
