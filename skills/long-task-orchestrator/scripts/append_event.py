#!/usr/bin/env python3
"""
append_event.py — 向 events.jsonl 追加一条不可变事件

用法:
  python3 append_event.py <task_dir> <event_type> [--data <json>]

示例:
  python3 append_event.py /path/to/TASK-xxx "USER_CONFIRMED" \
    --data '{"batch_id":"BATCH-001","confirmed_by":"admin"}'

职责:
  - 仅追加（append-only），不修改已有事件
  - 自动记录时间戳
  - 支持结构化 JSON 数据
"""

import json
import os
import sys
from datetime import datetime, timezone


def main():
    if len(sys.argv) < 3:
        print("用法: append_event.py <task_dir> <event_type> [--data <json>]", file=sys.stderr)
        sys.exit(2)

    task_dir = sys.argv[1]
    event_type = sys.argv[2]

    # 解析 --data
    event_data = {}
    i = 3
    while i < len(sys.argv):
        if sys.argv[i] == "--data" and i + 1 < len(sys.argv):
            try:
                event_data = json.loads(sys.argv[i + 1])
            except json.JSONDecodeError as e:
                print(f"ERROR: --data JSON 解析失败: {e}", file=sys.stderr)
                sys.exit(1)
            i += 2
        else:
            i += 1

    task_id = task_dir.split("/")[-1].split("_")[0]

    event = {
        "event": event_type,
        "task_id": task_id,
        "time": datetime.now(timezone.utc).isoformat(),
        **event_data,
    }

    events_path = os.path.join(task_dir, "04_events.jsonl")
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    print(json.dumps({"status": "ok", "event": event_type, "time": event["time"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
