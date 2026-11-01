#!/usr/bin/env python3
"""
reconcile_task.py — 对账：统计各状态数量，判断任务总状态

用法:
  python3 reconcile_task.py <task_dir>

输出:
  - planned_count / accounted_count / success_count / exception_count / remaining_count
  - 是否 accounted_for（所有工作项均有终态）
  - 建议的任务总状态（COMPLETED / COMPLETED_WITH_EXCEPTIONS / 仍需继续）

终态集合:
  SUCCEEDED, FAILED_FINAL, NEEDS_REVIEW,
  NO_ACCESS, NOT_FOUND, VERSION_CONFLICT, SKIPPED, CANCELLED

非终态:
  PENDING, READY, CLAIMED, RUNNING, FAILED_RETRYABLE
"""

import json
import os
import sys
from datetime import datetime, timezone


TERMINAL_STATES = {
    "SUCCEEDED", "FAILED_FINAL", "NEEDS_REVIEW",
    "NO_ACCESS", "NOT_FOUND", "VERSION_CONFLICT",
    "SKIPPED", "CANCELLED",
}


def main():
    if len(sys.argv) < 2:
        print("用法: reconcile_task.py <task_dir>", file=sys.stderr)
        sys.exit(2)

    task_dir = sys.argv[1]
    items_path = os.path.join(task_dir, "03_work_items.jsonl")

    if not os.path.exists(items_path):
        print(json.dumps({"status": "error", "error": "work_items.jsonl 不存在"}))
        sys.exit(1)

    counts = {}
    total = 0
    with open(items_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                st = item.get("status", "PENDING")
                counts[st] = counts.get(st, 0) + 1
                total += 1
            except json.JSONDecodeError:
                pass

    success_count = counts.get("SUCCEEDED", 0)
    exception_count = sum(
        counts.get(s, 0) for s in (
            "FAILED_FINAL", "NEEDS_REVIEW", "NO_ACCESS",
            "NOT_FOUND", "VERSION_CONFLICT",
        )
    )
    terminal_count = sum(counts.get(s, 0) for s in TERMINAL_STATES)
    accounted_for = (terminal_count == total) and (total > 0)

    remaining_items = total - terminal_count

    if total == 0:
        task_status = "CREATED"
    elif accounted_for and success_count == total:
        task_status = "COMPLETED"
    elif accounted_for:
        task_status = "COMPLETED_WITH_EXCEPTIONS"
    elif remaining_items > 0:
        task_status = "RUNNING"
    else:
        task_status = "PARTIALLY_COMPLETED"

    report = {
        "task_id": task_dir.split("/")[-1].split("_")[0],
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
        "planned_count": total,
        "accounted_for": accounted_for,
        "success_count": success_count,
        "exception_count": exception_count,
        "remaining_count": remaining_items,
        "suggested_task_status": task_status,
        "counts_by_status": counts,
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
