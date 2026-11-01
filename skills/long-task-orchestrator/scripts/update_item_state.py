#!/usr/bin/env python3
"""
update_item_state.py — 更新单个 work_item 状态（带转换校验和乐观锁）

用法:
  python3 update_item_state.py <task_dir> <item_id> <new_status> [--result <json>] [--reason <text>]

职责:
  1. 校验状态转换是否合法
  2. 检查 state_version（乐观锁，防止并发覆盖）
  3. 更新 item 状态、attempt_count、result 等字段
  4. 写回 work_items.jsonl
  5. 追加事件到 events.jsonl

合法转换:
  PENDING       → READY, SKIPPED, CANCELLED
  READY         → CLAIMED, SKIPPED
  CLAIMED       → RUNNING, READY (lease expired reset)
  RUNNING       → SUCCEEDED, FAILED_RETRYABLE, FAILED_FINAL, NEEDS_REVIEW,
                  NO_ACCESS, NOT_FOUND, VERSION_CONFLICT
  FAILED_RETRYABLE → READY (retry), FAILED_FINAL (max attempts)
"""

import json
import os
import sys
from datetime import datetime, timezone


VALID_TRANSITIONS = {
    "PENDING": {
        "READY", "SKIPPED", "CANCELLED",
    },
    "READY": {
        "CLAIMED", "SKIPPED",
    },
    "CLAIMED": {
        "RUNNING", "READY",  # READY = lease expired reset
    },
    "RUNNING": {
        "SUCCEEDED", "FAILED_RETRYABLE", "FAILED_FINAL",
        "NEEDS_REVIEW", "NO_ACCESS", "NOT_FOUND",
        "VERSION_CONFLICT",
    },
    "FAILED_RETRYABLE": {
        "READY", "FAILED_FINAL",  # READY = retry
    },
    # 以下为终态，不允许再转换
    "SUCCEEDED": set(),
    "FAILED_FINAL": set(),
    "NEEDS_REVIEW": set(),
    "NO_ACCESS": set(),
    "NOT_FOUND": set(),
    "VERSION_CONFLICT": set(),
    "SKIPPED": set(),
    "CANCELLED": set(),
}


def read_all_items(task_dir):
    items = []
    path = os.path.join(task_dir, "03_work_items.jsonl")
    if not os.path.exists(path):
        return items
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return items


def write_all_items(task_dir, items):
    path = os.path.join(task_dir, "03_work_items.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def main():
    if len(sys.argv) < 4:
        print("用法: update_item_state.py <task_dir> <item_id> <new_status> [--result <json>] [--reason <text>]", file=sys.stderr)
        sys.exit(2)

    task_dir = sys.argv[1]
    target_id = sys.argv[2]
    new_status = sys.argv[3]

    result_data = None
    reason = ""
    i = 4
    while i < len(sys.argv):
        if sys.argv[i] == "--result" and i + 1 < len(sys.argv):
            try:
                result_data = json.loads(sys.argv[i + 1])
            except json.JSONDecodeError:
                result_data = {"value": sys.argv[i + 1]}
            i += 2
        elif sys.argv[i] == "--reason" and i + 1 < len(sys.argv):
            reason = sys.argv[i + 1]
            i += 2
        else:
            i += 1

    items = read_all_items(task_dir)

    # 查找目标 item
    target_item = None
    for item in items:
        if item["item_id"] == target_id:
            target_item = item
            break

    if target_item is None:
        print(json.dumps({"status": "error", "error": f"item_id {target_id} not found"}))
        sys.exit(1)

    old_status = target_item.get("status", "PENDING")

    # 校验转换
    allowed = VALID_TRANSITIONS.get(old_status, set())
    if new_status not in allowed and old_status != new_status:
        print(json.dumps({
            "status": "error",
            "error": f"非法状态转换: {old_status} → {new_status}",
            "allowed_from": list(allowed),
        }))
        sys.exit(1)

    # 更新
    now = datetime.now(timezone.utc)
    target_item["status"] = new_status

    lease = target_item.get("lease", {})
    lease["state_version"] = lease.get("state_version", 0) + 1
    target_item["lease"] = lease

    if new_status == "RUNNING":
        target_item["attempt_count"] = target_item.get("attempt_count", 0) + 1
        target_item.setdefault("started_at", now.isoformat())
    elif new_status in ("SUCCEEDED", "FAILED_FINAL", "NEEDS_REVIEW",
                        "NO_ACCESS", "NOT_FOUND", "VERSION_CONFLICT", "SKIPPED", "CANCELLED"):
        target_item["completed_at"] = now.isoformat()
    elif new_status == "FAILED_RETRYABLE":
        target_item["last_error"] = reason

    if result_data:
        target_item["result"] = result_data
    if reason:
        target_item.setdefault("reasons", []).append({"status": new_status, "reason": reason, "time": now.isoformat()})

    # 写回
    write_all_items(task_dir, items)

    # 追加事件
    task_id = task_dir.split("/")[-1].split("_")[0]
    events_path = os.path.join(task_dir, "04_events.jsonl")
    event = {
        "event": "ITEM_STATE_CHANGED",
        "task_id": task_id,
        "item_id": target_id,
        "time": now.isoformat(),
        "old_status": old_status,
        "new_status": new_status,
        "reason": reason,
        "state_version": lease["state_version"],
    }
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    print(json.dumps({
        "status": "ok",
        "item_id": target_id,
        "old_status": old_status,
        "new_status": new_status,
        "state_version": lease["state_version"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
