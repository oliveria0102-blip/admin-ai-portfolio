#!/usr/bin/env python3
"""
resume_task.py — 从保存状态恢复任务执行

用法:
  python3 resume_task.py <task_dir>

职责:
  1. 读取 manifest + work_items + events
  2. 检查 IN_PROGRESS / CLAIMED 项的租约是否过期
  3. 检查 idempotency_key → 是否已有完成事件/结果文件
  4. 输出：可恢复/需重试/需人工处理 的分类清单

输出各 item 的推荐动作:
  - RESUME  : 租约有效，继续当前执行
  - CONFIRM : 发现已完成的结果，标记为 SUCCEEDED
  - RETRY   : 租约过期且无结果，重置为 READY
  - REVIEW  : 需要人工判断（结果存在但状态未更新等）
  - RETRY_EXPIRED : FAILED_RETRYABLE 且租约过期
"""

import json
import os
import sys
from datetime import datetime, timezone


def parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def has_result_in_batches(task_dir, item_id):
    """检查 batches 目录中是否有该 item 的结果"""
    batches_dir = os.path.join(task_dir, "07_batches")
    if not os.path.isdir(batches_dir):
        return False
    for fname in os.listdir(batches_dir):
        if fname.endswith("_result.jsonl"):
            path = os.path.join(batches_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        if item_id in line:
                            return True
            except Exception:
                pass
    return False


def main():
    if len(sys.argv) < 2:
        print("用法: resume_task.py <task_dir>", file=sys.stderr)
        sys.exit(2)

    task_dir = sys.argv[1]
    items_path = os.path.join(task_dir, "03_work_items.jsonl")
    manifest_path = os.path.join(task_dir, "00_manifest.json")

    if not os.path.exists(items_path):
        print(json.dumps({"status": "error", "error": "work_items.jsonl 不存在，无法恢复"}))
        sys.exit(1)

    task_id = task_dir.split("/")[-1].split("_")[0]
    if os.path.exists(manifest_path):
        with open(manifest_path, "r") as f:
            task_id = json.load(f).get("task_id", task_id)

    now = datetime.now(timezone.utc)
    actions = []
    summary = {"total": 0, "resume": 0, "confirm": 0, "retry": 0, "review": 0, "terminal": 0}

    with open(items_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            summary["total"] += 1
            item_id = item["item_id"]
            status = item.get("status", "PENDING")
            lease = item.get("lease", {})

            # 终态 → 无需恢复
            if status in ("SUCCEEDED", "FAILED_FINAL", "NEEDS_REVIEW",
                          "NO_ACCESS", "NOT_FOUND", "VERSION_CONFLICT",
                          "SKIPPED", "CANCELLED"):
                summary["terminal"] += 1
                continue

            # PENDING / READY → 正常等待
            if status in ("PENDING", "READY"):
                continue

            # CLAIMED / RUNNING → 检查租约
            if status in ("CLAIMED", "RUNNING"):
                expires_str = lease.get("expires_at")
                expired = True
                if expires_str:
                    expires = parse_iso(expires_str)
                    if expires and now <= expires:
                        expired = False

                if not expired:
                    # 租约有效，检查是否已有结果
                    if has_result_in_batches(task_dir, item_id):
                        actions.append({
                            "item_id": item_id,
                            "action": "CONFIRM",
                            "reason": "result found, lease still valid → should mark SUCCEEDED",
                        })
                        summary["confirm"] += 1
                    else:
                        actions.append({
                            "item_id": item_id,
                            "action": "RESUME",
                            "reason": "lease valid, no result yet → continue",
                            "claimed_by": lease.get("claimed_by"),
                        })
                        summary["resume"] += 1
                else:
                    # 租约过期
                    if has_result_in_batches(task_dir, item_id):
                        actions.append({
                            "item_id": item_id,
                            "action": "CONFIRM",
                            "reason": "lease expired but result found → should mark SUCCEEDED",
                        })
                        summary["confirm"] += 1
                    else:
                        actions.append({
                            "item_id": item_id,
                            "action": "RETRY",
                            "reason": "lease expired, no result → reset to READY",
                        })
                        summary["retry"] += 1
                continue

            # FAILED_RETRYABLE → 检查尝试次数
            if status == "FAILED_RETRYABLE":
                attempts = item.get("attempt_count", 0)
                if attempts >= 3:  # 默认最大重试
                    actions.append({
                        "item_id": item_id,
                        "action": "REVIEW",
                        "reason": f"max retries ({attempts}/3) → should mark FAILED_FINAL",
                    })
                    summary["review"] += 1
                else:
                    actions.append({
                        "item_id": item_id,
                        "action": "RETRY",
                        "reason": f"FAILED_RETRYABLE ({attempts}/3) → can retry",
                    })
                    summary["retry"] += 1

    report = {
        "task_id": task_id,
        "resumed_at": now.isoformat(),
        "summary": summary,
        "actions": actions,
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
