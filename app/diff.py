"""轮次对比：根据记录编号列出新增、内容变化、消失的记录。

只有完整采完的轮次才能参与“消失”判断；进行中或失败的轮次不产出消失结论。
"""
from __future__ import annotations

import json

from .db import Database


def _records_map(db: Database, round_id: int) -> dict[str, dict]:
    rows = db.query_all(
        "SELECT record_id, data_json FROM round_records WHERE round_id=?",
        (round_id,))
    return {r["record_id"]: json.loads(r["data_json"]) for r in rows}


def diff_rounds(db: Database, task_id: int, round_id: int) -> dict:
    """对比指定轮次与它之前最近一个完整轮次。"""
    current = db.get_round(round_id)
    if current is None:
        raise ValueError("轮次不存在")
    if current["status"] != "completed":
        raise ValueError("只有完整采完的轮次才能进行对比（未采完不能判断记录消失）")

    prev = db.get_latest_completed_round(task_id, before_round=current["round_no"])
    cur_map = _records_map(db, round_id)
    prev_map = _records_map(db, prev["id"]) if prev else {}

    added, changed, unchanged, removed = [], [], [], []
    for rid, data in cur_map.items():
        if rid not in prev_map:
            added.append(rid)
        elif json.dumps(prev_map[rid], sort_keys=True, ensure_ascii=False) != \
                json.dumps(data, sort_keys=True, ensure_ascii=False):
            changed.append(rid)
        else:
            unchanged.append(rid)
    if prev is not None:
        removed = [rid for rid in prev_map if rid not in cur_map]

    return {
        "task_id": task_id,
        "round_id": round_id,
        "round_no": current["round_no"],
        "base_round_id": prev["id"] if prev else None,
        "base_round_no": prev["round_no"] if prev else None,
        "summary": {"added": len(added), "changed": len(changed),
                    "removed": len(removed), "unchanged": len(unchanged)},
        "added": sorted(added),
        "changed": sorted(changed),
        "removed": sorted(removed),
    }
