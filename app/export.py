"""导出 CSV / JSON。

- CSV 使用 utf-8-sig（Excel 直接打开中文不乱码），字段内含逗号、引号、换行均按 RFC4180 转义。
- JSON 导出该轮保存的完整原始记录快照。历史轮次的快照独立存储，后续再采集不会改变旧导出。
"""
from __future__ import annotations

import csv
import io
import json

from .db import Database
from .utils import get_nested


def _iter_round_records(db: Database, round_id: int):
    last_id = 0
    while True:
        rows = db.query_all(
            "SELECT * FROM round_records WHERE round_id=? AND id>? ORDER BY id LIMIT 500",
            (round_id, last_id))
        if not rows:
            return
        for row in rows:
            last_id = row["id"]
            yield row


def export_csv(db: Database, round_id: int, fields: list[dict]) -> bytes:
    """fields: [{'path': 'id', 'column': '编号'}, {'path': 'author.name', ...}]"""
    buf = io.StringIO()
    headers = [f.get("column") or f.get("path") for f in fields] or ["id"]
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    for row in _iter_round_records(db, round_id):
        data = json.loads(row["data_json"])
        values = []
        for f in (fields or [{"path": "id", "column": "id"}]):
            value = get_nested(data, f["path"])
            if value is None:
                values.append("")  # 没有的字段留空
            elif isinstance(value, (dict, list)):
                values.append(json.dumps(value, ensure_ascii=False))
            else:
                values.append(str(value))
        writer.writerow(values)
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def export_json(db: Database, round_id: int) -> bytes:
    records = []
    for row in _iter_round_records(db, round_id):
        records.append(json.loads(row["data_json"]))
    return json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
