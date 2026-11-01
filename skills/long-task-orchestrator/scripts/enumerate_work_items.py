#!/usr/bin/env python3
"""
enumerate_work_items.py — 从 scope_snapshot 生成 work_items.jsonl

用法:
  python3 enumerate_work_items.py <task_dir> [--item-type <type>] [--from-file <file>]

职责:
  读取 scope_snapshot.json，按 item_type 和 source_ref 拆分出 work_item 列表，
  追加写入 03_work_items.jsonl（幂等：item_id 已存在则跳过）。

支持的 item_type:
  ASSET, PERSON_RECORD, ORGANIZATION, DOCUMENT_SECTION,
  SPREADSHEET_ROW, FACT, DELIVERABLE, CHANGE_ITEM, CUSTOM
"""

import json
import os
import sys
from datetime import datetime, timezone


def load_existing_items(task_dir):
    """读取已有 work_items.jsonl 中的 item_id 集合"""
    existing = set()
    path = os.path.join(task_dir, "03_work_items.jsonl")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        item = json.loads(line)
                        existing.add(item.get("item_id"))
                    except json.JSONDecodeError:
                        pass
    return existing


def make_idempotency_key(task_id, item_id, source_version=""):
    return f"{task_id}:{item_id}:{source_version}"


def make_lease():
    return {
        "claimed_by": None,
        "claimed_at": None,
        "expires_at": None,
        "state_version": 0,
    }


def generate_items_from_scope(task_id, scope, item_type=None, existing_ids=None):
    """从 scope 描述生成 work_item 列表"""
    if existing_ids is None:
        existing_ids = set()

    items = []
    sources = scope.get("sources", scope.get("baseline_assets", []))

    # 如果 scope 有 item_count 和 item_type，按数量生成
    item_count = scope.get("item_count", scope.get("estimated_count", 0))
    specified_type = item_type or scope.get("item_type", "ASSET")

    if isinstance(sources, list) and item_count == 0:
        # 按资产拆分
        for i, src in enumerate(sources):
            item_id = f"ITEM-{i+1:04d}"
            if item_id in existing_ids:
                continue
            items.append({
                "item_id": item_id,
                "item_type": specified_type,
                "source_ref": src.get("file_id", src.get("asset_id", str(i))),
                "source_name": src.get("name", src.get("asset_name", "")),
                "owner_skill": scope.get("owner_skill", ""),
                "status": "PENDING",
                "attempt_count": 0,
                "idempotency_key": make_idempotency_key(task_id, item_id),
                "lease": make_lease(),
            })
    elif item_count > 0:
        # 按预估数量生成（如 200 人 → 200 个 PERSON_RECORD）
        for i in range(1, item_count + 1):
            item_id = f"ITEM-{i:04d}"
            if item_id in existing_ids:
                continue
            items.append({
                "item_id": item_id,
                "item_type": specified_type,
                "source_ref": scope.get("source_file", sources[0].get("file_id", "") if sources else ""),
                "source_name": scope.get("source_name", ""),
                "owner_skill": scope.get("owner_skill", ""),
                "status": "PENDING",
                "attempt_count": 0,
                "idempotency_key": make_idempotency_key(task_id, item_id),
                "lease": make_lease(),
            })

    return items


def main():
    if len(sys.argv) < 2:
        print("用法: enumerate_work_items.py <task_dir> [--item-type <type>] [--from-file <scope_file>]", file=sys.stderr)
        sys.exit(2)

    task_dir = sys.argv[1]

    # 解析参数
    item_type = None
    scope_file = None
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--item-type" and i + 1 < len(sys.argv):
            item_type = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--from-file" and i + 1 < len(sys.argv):
            scope_file = sys.argv[i + 1]
            i += 2
        else:
            i += 1

    # 读取 scope snapshot
    snapshot_path = scope_file or os.path.join(task_dir, "02_scope_snapshot.json")
    if not os.path.exists(snapshot_path):
        print(f"ERROR: scope snapshot 不存在: {snapshot_path}", file=sys.stderr)
        sys.exit(1)

    with open(snapshot_path, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    scope = snapshot.get("scope", snapshot)

    # 读取 manifest 获取 task_id
    manifest_path = os.path.join(task_dir, "00_manifest.json")
    task_id = task_dir.split("/")[-1].split("_")[0]  # fallback
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            task_id = json.load(f).get("task_id", task_id)

    # 生成 items
    existing_ids = load_existing_items(task_dir)
    items = generate_items_from_scope(task_id, scope, item_type, existing_ids)

    if not items:
        print(json.dumps({"status": "no_new_items", "count": 0}))
        sys.exit(0)

    # 追加写入
    work_items_path = os.path.join(task_dir, "03_work_items.jsonl")
    count = 0
    with open(work_items_path, "a", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            count += 1

    # 记录事件
    events_path = os.path.join(task_dir, "04_events.jsonl")
    event = {
        "event": "ITEMS_ENUMERATED",
        "task_id": task_id,
        "time": datetime.now(timezone.utc).isoformat(),
        "count": count,
    }
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    print(json.dumps({"status": "ok", "new_items": count, "item_type": item_type or scope.get("item_type", "ASSET")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
