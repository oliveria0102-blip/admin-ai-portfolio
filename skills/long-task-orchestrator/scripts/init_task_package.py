#!/usr/bin/env python3
"""
init_task_package.py — 初始化长任务包

用法:
  python3 init_task_package.py <task_contract.json> [--output-dir <dir>] [--dry-run]

职责:
  1. 校验 TaskContract v1.1 字段完整性
  2. 生成不冲突的任务 ID（日期 + 4位随机短码）
  3. 创建任务包目录结构（JSONL 为主、人工快照为辅）
  4. 写入 manifest.json 和 scope_snapshot.json
"""

import json
import os
import random
import string
import sys
from datetime import datetime, timezone


REQUIRED_CONTRACT_FIELDS = [
    "state",
    "objective",
    "scope",
    "routing.owner_skill",
    "routing.orchestration_required",
]

DIR_STRUCTURE = [
    "07_batches",
    "08_reports",
    "09_attachments",
]


def generate_task_id(date_str=None):
    """生成 TASK-YYYYMMDD-RAND4_简称 格式的 ID"""
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"TASK-{date_str}-{code}"


def validate_contract(contract):
    """校验 TaskContract v1.1 字段完整性，返回 (ok, errors)"""
    errors = []

    if contract.get("state") != "CONFIRMED":
        errors.append("TaskContract.state 必须为 CONFIRMED")

    routing = contract.get("routing", {})
    if not routing.get("orchestration_required"):
        errors.append("routing.orchestration_required 必须为 true")

    owner = routing.get("owner_skill")
    if not owner:
        errors.append("routing.owner_skill 不能为空")
    elif owner == "long-task-orchestrator":
        errors.append("routing.owner_skill 不能是编排器自身（会导致递归）")

    if not contract.get("objective"):
        errors.append("objective 不能为空")

    scope = contract.get("scope")
    if not scope:
        errors.append("scope 不能为空")

    return len(errors) == 0, errors


def create_manifest(task_id, contract, output_dir):
    """创建 manifest.json"""
    manifest = {
        "task_id": task_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "contract_version": "v1.1",
        "objective": contract.get("objective"),
        "owner_skill": contract.get("routing", {}).get("owner_skill"),
        "output_dir": output_dir,
        "status": "CREATED",
        "locked_by": None,
        "locked_at": None,
    }
    with open(os.path.join(output_dir, "00_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def create_scope_snapshot(contract, output_dir):
    """创建 scope_snapshot.json"""
    scope = contract.get("scope", {})
    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "sources": contract.get("sources", {}),
        "operations": contract.get("operations", []),
    }
    with open(os.path.join(output_dir, "02_scope_snapshot.json"), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return snapshot


def main():
    dry_run = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if len(args) < 1:
        print("用法: init_task_package.py <task_contract.json> [--output-dir <dir>] [--dry-run]", file=sys.stderr)
        sys.exit(2)

    contract_path = args[0]

    # 读取合同
    try:
        with open(contract_path, "r", encoding="utf-8") as f:
            contract = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"ERROR: 无法读取合同文件: {e}", file=sys.stderr)
        sys.exit(1)

    # 校验
    ok, errors = validate_contract(contract)
    if not ok:
        print("TaskContract 校验失败:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    # 生成任务 ID
    task_id = generate_task_id()
    short_name = contract.get("objective", "task")[:20].replace(" ", "_")
    task_dir_name = f"{task_id}_{short_name}"

    # 确定输出目录
    output_idx = None
    for i, a in enumerate(sys.argv):
        if a == "--output-dir" and i + 1 < len(sys.argv):
            output_idx = i + 1
            break
    base_dir = sys.argv[output_idx] if output_idx else "/workspace"
    output_dir = os.path.join(base_dir, task_dir_name)

    if dry_run:
        print(f"[DRY RUN] 将创建: {output_dir}")
        print(f"[DRY RUN] 任务 ID: {task_id}")
        sys.exit(0)

    # 创建目录结构
    os.makedirs(output_dir, exist_ok=True)
    for subdir in DIR_STRUCTURE:
        os.makedirs(os.path.join(output_dir, subdir), exist_ok=True)

    # 写入 manifest
    manifest = create_manifest(task_id, contract, output_dir)

    # 写入 scope snapshot
    create_scope_snapshot(contract, output_dir)

    # 写入合同副本
    with open(os.path.join(output_dir, "01_task_contract.json"), "w", encoding="utf-8") as f:
        json.dump(contract, f, ensure_ascii=False, indent=2)

    # 初始化空的 JSONL 文件
    for name in ["03_work_items.jsonl", "04_events.jsonl", "05_exceptions.jsonl"]:
        open(os.path.join(output_dir, name), "w", encoding="utf-8").close()

    # 记录创建事件
    event = {
        "event": "TASK_CREATED",
        "task_id": task_id,
        "time": datetime.now(timezone.utc).isoformat(),
        "owner_skill": manifest["owner_skill"],
    }
    with open(os.path.join(output_dir, "04_events.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    print(json.dumps({
        "task_id": task_id,
        "output_dir": output_dir,
        "status": "CREATED",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
