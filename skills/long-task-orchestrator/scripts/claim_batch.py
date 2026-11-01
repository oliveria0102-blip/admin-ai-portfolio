#!/usr/bin/env python3
"""
claim_batch.py — 从 work_items.jsonl 中原子领取一批工作项

用法:
  python3 claim_batch.py <task_dir> [--batch-size <N>] [--claimed-by <id>] [--lease-minutes <M>]

职责:
  1. 读取 03_work_items.jsonl，筛选 PENDING 项
  2. 检查已有租约是否过期（过期 → 释放）
  3. 为选定项写入租约（claimed_by, claimed_at, expires_at, state_version++)
  4. 生成 BatchExecutionEnvelope 写入 07_batches/
  5. 记录 CLAIMED 事件

并发安全:
  - 同一 task_dir 不应被两个进程同时 claim（依赖文件锁的场景待接入 Python flock）
  - 当前版本依赖调用方保证单进程操作同一任务包
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone


DEFAULT_BATCH_SIZE = 3
DEFAULT_LEASE_MINUTES = 30


def parse_iso(s):
    """安全解析 ISO 时间字符串"""
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def is_lease_expired(item):
    """检查租约是否已过期"""
    lease = item.get("lease", {})
    expires_str = lease.get("expires_at")
    if not expires_str:
        return True  # 没有租约 = 可领取
    expires = parse_iso(expires_str)
    if expires is None:
        return True
    return datetime.now(timezone.utc) > expires


def read_all_items(task_dir):
    """读取全部 work_item"""
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
    """全量写回 work_items.jsonl（当前简化实现）"""
    path = os.path.join(task_dir, "03_work_items.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def get_next_batch_number(task_dir):
    """获取下一个批次编号"""
    batches_dir = os.path.join(task_dir, "07_batches")
    existing = [f for f in os.listdir(batches_dir) if f.startswith("batch_")]
    return len(existing) + 1


def main():
    task_dir = sys.argv[1] if len(sys.argv) > 1 else None
    if not task_dir or not os.path.isdir(task_dir):
        print("用法: claim_batch.py <task_dir> [--batch-size <N>] [--claimed-by <id>] [--lease-minutes <M>]", file=sys.stderr)
        sys.exit(2)

    batch_size = DEFAULT_BATCH_SIZE
    claimed_by = os.environ.get("USER", "agent")
    lease_minutes = DEFAULT_LEASE_MINUTES

    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--batch-size" and i + 1 < len(sys.argv):
            batch_size = int(sys.argv[i + 1])
            i += 2
        elif sys.argv[i] == "--claimed-by" and i + 1 < len(sys.argv):
            claimed_by = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--lease-minutes" and i + 1 < len(sys.argv):
            lease_minutes = int(sys.argv[i + 1])
            i += 2
        else:
            i += 1

    # 读取所有 items
    items = read_all_items(task_dir)

    # 筛选可领取的 PENDING 项（含过期租约的 CLAIMED/RUNNING 项）
    now = datetime.now(timezone.utc)
    lease_expiry = now + timedelta(minutes=lease_minutes)

    available = []
    for item in items:
        status = item.get("status")
        if status == "PENDING":
            available.append(item)
        elif status in ("CLAIMED", "RUNNING") and is_lease_expired(item):
            # 租约已过期，可回收
            item["status"] = "PENDING"
            item["lease"] = {
                "claimed_by": None,
                "claimed_at": None,
                "expires_at": None,
                "state_version": item.get("lease", {}).get("state_version", 0) + 1,
            }
            available.append(item)

    if not available:
        print(json.dumps({"status": "no_items_available", "claimed": 0}))
        sys.exit(0)

    # 取 batch_size 个
    selected = available[:batch_size]
    batch_num = get_next_batch_number(task_dir)
    batch_id = f"BATCH-{batch_num:03d}"

    # 写租约
    task_id = task_dir.split("/")[-1].split("_")[0]
    for item in selected:
        item["status"] = "CLAIMED"
        item["lease"] = {
            "claimed_by": claimed_by,
            "claimed_at": now.isoformat(),
            "expires_at": lease_expiry.isoformat(),
            "state_version": item.get("lease", {}).get("state_version", 0) + 1,
        }

    # 写回 work_items
    write_all_items(task_dir, items)

    # 生成 BatchExecutionEnvelope
    envelope = {
        "batch_id": batch_id,
        "task_id": task_id,
        "owner_skill": selected[0].get("owner_skill", "unknown"),
        "orchestration_context": {
            "owner": "long-task-orchestrator",
            "task_id": task_id,
            "batch_id": batch_id,
            "recursion_forbidden": True,
        },
        "work_items": [
            {
                "item_id": it["item_id"],
                "item_type": it["item_type"],
                "input_ref": it["source_ref"],
            }
            for it in selected
        ],
        "required_output_schema": "BatchItemResult-v1",
    }

    # 写入批次请求文件
    envelope_path = os.path.join(task_dir, "07_batches", f"{batch_id}_request.json")
    with open(envelope_path, "w", encoding="utf-8") as f:
        json.dump(envelope, f, ensure_ascii=False, indent=2)

    # 记录事件
    events_path = os.path.join(task_dir, "04_events.jsonl")
    event = {
        "event": "BATCH_CLAIMED",
        "task_id": task_id,
        "batch_id": batch_id,
        "time": now.isoformat(),
        "claimed_by": claimed_by,
        "item_ids": [it["item_id"] for it in selected],
        "lease_expires_at": lease_expiry.isoformat(),
    }
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    print(json.dumps({
        "status": "ok",
        "batch_id": batch_id,
        "claimed": len(selected),
        "item_ids": [it["item_id"] for it in selected],
        "envelope": envelope_path,
        "lease_expires_at": lease_expiry.isoformat(),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
