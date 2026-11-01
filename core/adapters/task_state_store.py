# -*- coding: utf-8 -*-
"""TaskStateStore — TaskState 存储抽象（方案 V0.2 §22）。

生产优先接 task-state MCP；POC 后端为本地 JSON。控制器只依赖本模块的
接口语义，不得直接 open 状态文件。所有更新走 expected_version 乐观锁，
多人/多 Agent 并发以 STATE_CONFLICT 显式失败，禁止静默覆盖。
"""
import json
import os
import time

STATE_CONFLICT = "STATE_CONFLICT"

STATE_SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "protocols", "task_state_v1.schema.json")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class LocalJsonStore:
    """POC 后端：单活跃任务 JSON + 迁移历史 JSONL（append-only）。"""

    def __init__(self, runtime_dir):
        self.runtime_dir = runtime_dir
        os.makedirs(runtime_dir, exist_ok=True)
        self.state_path = os.path.join(runtime_dir, "task_state.json")
        self.transitions_path = os.path.join(runtime_dir, "transitions.jsonl")

    # -- 读 --
    def get_active_task(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def get_state(self, task_id):
        st = self.get_active_task()
        if st and st.get("task_id") == task_id:
            return st
        return None

    # -- 写（乐观锁） --
    def create_task(self, seed):
        """seed: 已通过上层校验的初始字段 dict；自动补 runtime/version。"""
        state = dict(seed)
        state.setdefault("schema_version", "1.0")
        state["state_version"] = 1
        state.setdefault("status", "ACTIVE")
        state.setdefault("runtime", {})
        state["runtime"].setdefault("current_turn", 0)
        state["runtime"].setdefault("policy_version", "harness-v0.2")
        state["runtime"].setdefault("stop_block_count", 0)
        state["runtime"]["updated_at"] = _now()
        self._atomic_write(state)
        return state

    def compare_and_update(self, task_id, expected_version, mutate):
        """mutate(dict) -> dict；版本不符返回 STATE_CONFLICT。"""
        state = self.get_active_task()
        if not state or state.get("task_id") != task_id:
            return STATE_CONFLICT
        if state.get("state_version") != expected_version:
            return STATE_CONFLICT
        new_state = mutate(json.loads(json.dumps(state, ensure_ascii=False)))
        new_state["state_version"] = expected_version + 1
        new_state.setdefault("runtime", {})["updated_at"] = _now()
        self._atomic_write(new_state)
        return new_state

    def clear_active(self, task_id):
        state = self.get_active_task()
        if state and state.get("task_id") == task_id:
            if os.path.exists(self.state_path):
                os.remove(self.state_path)

    # -- 迁移历史 --
    def append_transition(self, transition):
        with open(self.transitions_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(transition, ensure_ascii=False) + "\n")

    def list_transitions(self, task_id=None):
        out = []
        try:
            with open(self.transitions_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    if task_id is None or rec.get("task_id") == task_id:
                        out.append(rec)
        except Exception:
            pass
        return out

    def _atomic_write(self, state):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)


class SqliteStore:
    """V0.4-A 生产后端：SQLite（WAL）。

    接口语义与 LocalJsonStore 严格对拍（tests/unit/test_sqlite_store.py）：
    - get_active_task = 最新一行任务（按 rowid 序），无论 status——对齐
      "状态文件存在即返回" 的 JSON 语义；
    - create_task 插入新行（历史任务保留，供 MCP 查询面/巡检），JSON 模型
      覆盖文件丢历史的行为在此升级为追加，不影响现有消费方；
    - compare_and_update 事务内二次校验版本（BEGIN IMMEDIATE），冲突返回
      STATE_CONFLICT，与 JSON 后端 fail-open 语义一致；
    - WAL + busy_timeout=5000 应对 hooks 多进程并发。
    """

    DDL = """
    CREATE TABLE IF NOT EXISTS tasks (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL UNIQUE,
        state_json TEXT NOT NULL,
        state_version INTEGER NOT NULL,
        status TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS transitions (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        transition_id TEXT NOT NULL UNIQUE,
        task_id TEXT NOT NULL,
        from_version INTEGER,
        to_version INTEGER,
        event_json TEXT NOT NULL,
        transition_type TEXT NOT NULL,
        changes_json TEXT NOT NULL,
        reason_code TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_transitions_task
        ON transitions(task_id, seq);
    """

    def __init__(self, runtime_dir):
        import sqlite3
        self.runtime_dir = runtime_dir
        os.makedirs(runtime_dir, exist_ok=True)
        self.db_path = os.path.join(runtime_dir, "task_state.db")
        self._conn = sqlite3.connect(self.db_path, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(self.DDL)
        self._conn.commit()

    # -- 读 --
    def get_active_task(self):
        row = self._conn.execute(
            "SELECT state_json FROM tasks ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return json.loads(row["state_json"])

    def get_state(self, task_id):
        row = self._conn.execute(
            "SELECT state_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def list_tasks(self, status=None, limit=50):
        """SqliteStore 扩展能力（LocalJsonStore 无）：历史任务列表。"""
        q = ("SELECT task_id, state_json, updated_at FROM tasks "
             "{where} ORDER BY seq DESC LIMIT ?")
        where, params = "", [limit]
        if status:
            where = "WHERE status=?"
            params = [status, limit]
        rows = self._conn.execute(q.format(where=where), params).fetchall()
        return [{"task_id": r["task_id"], "updated_at": r["updated_at"],
                 "state": json.loads(r["state_json"])} for r in rows]

    # -- 写（乐观锁） --
    def create_task(self, seed):
        state = dict(seed)
        state.setdefault("schema_version", "1.0")
        state["state_version"] = 1
        state.setdefault("status", "ACTIVE")
        state.setdefault("runtime", {})
        state["runtime"].setdefault("current_turn", 0)
        state["runtime"].setdefault("policy_version", "harness-v0.2")
        state["runtime"].setdefault("stop_block_count", 0)
        state["runtime"]["updated_at"] = _now()
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks"
                " (task_id, state_json, state_version, status, updated_at)"
                " VALUES (?,?,?,?,?)",
                (state["task_id"], json.dumps(state, ensure_ascii=False),
                 state["state_version"], state["status"],
                 state["runtime"]["updated_at"]))
        return state

    def insert_preserve(self, state):
        """迁移专用：按原样插入状态（保留 state_version 等全部字段，
        不做 create_task 的新任务默认化——V0.4-A 迁移缺陷修正：
        create_task 曾把已推进任务的 state_version 重置为 1）。"""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks"
                " (task_id, state_json, state_version, status, updated_at)"
                " VALUES (?,?,?,?,?)",
                (state["task_id"], json.dumps(state, ensure_ascii=False),
                 state.get("state_version", 1), state.get("status", "ACTIVE"),
                 (state.get("runtime") or {}).get("updated_at") or _now()))
        return state

    def compare_and_update(self, task_id, expected_version, mutate):
        cur = self._conn.execute(
            "SELECT state_json, state_version FROM tasks WHERE task_id=?",
            (task_id,)).fetchone()
        if not cur or cur["state_version"] != expected_version:
            return STATE_CONFLICT
        state = json.loads(cur["state_json"])
        new_state = mutate(json.loads(json.dumps(state, ensure_ascii=False)))
        new_version = expected_version + 1
        new_state["state_version"] = new_version
        new_state.setdefault("runtime", {})["updated_at"] = _now()
        # BEGIN IMMEDIATE 串行化"读-校验-写"，防多进程竞态窗口
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT state_version FROM tasks WHERE task_id=?",
                (task_id,)).fetchone()
            if not row or row["state_version"] != expected_version:
                self._conn.rollback()
                return STATE_CONFLICT
            self._conn.execute(
                "UPDATE tasks SET state_json=?, state_version=?, status=?,"
                " updated_at=? WHERE task_id=?",
                (json.dumps(new_state, ensure_ascii=False), new_version,
                 new_state.get("status", "ACTIVE"),
                 new_state["runtime"]["updated_at"], task_id))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return new_state

    def clear_active(self, task_id):
        with self._conn:
            self._conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))

    # -- 迁移历史 --
    def append_transition(self, transition):
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO transitions"
                " (transition_id, task_id, from_version, to_version,"
                "  event_json, transition_type, changes_json, reason_code,"
                "  created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (transition.get("transition_id"),
                 transition.get("task_id"),
                 transition.get("from_version"),
                 transition.get("to_version"),
                 json.dumps(transition.get("event") or {}, ensure_ascii=False),
                 transition.get("transition_type", ""),
                 json.dumps(transition.get("changes") or {}, ensure_ascii=False),
                 transition.get("reason_code", ""),
                 transition.get("created_at") or _now()))

    def list_transitions(self, task_id=None):
        q = ("SELECT transition_id, task_id, from_version, to_version,"
             " event_json, transition_type, changes_json, reason_code,"
             " created_at FROM transitions {where} ORDER BY seq ASC")
        where, params = "", []
        if task_id is not None:
            where = "WHERE task_id=?"
            params = [task_id]
        rows = self._conn.execute(q.format(where=where), params).fetchall()
        return [{
            "transition_id": r["transition_id"],
            "task_id": r["task_id"],
            "from_version": r["from_version"],
            "to_version": r["to_version"],
            "event": json.loads(r["event_json"]),
            "transition_type": r["transition_type"],
            "changes": json.loads(r["changes_json"]),
            "reason_code": r["reason_code"],
            "created_at": r["created_at"],
        } for r in rows]


class TaskStateMcpStore:
    """生产后端接口位（task-state MCP）。Phase 3.5 接入：create/transition/
    update_item/list_items 与 MCP 工具一一映射。控制器不得绑定本地后端。"""

    def __init__(self, *a, **kw):
        raise NotImplementedError("task-state MCP 后端在 Phase 3.5 接入")


def default_store(harness_root):
    # HARNESS_RUNTIME_DIR 覆盖：eval/integration 测试真隔离（V0.3-F），
    # 防止测试状态与 live 会话状态互踩（见 hooks/_harness_lib.py 注记）
    # V0.4-A 后端开关：默认 sqlite（2026-09-30 主人批准 live 切换）；
    # HARNESS_STORE_BACKEND=json 显式回退 JSON（回滚路径，JSON 文件只读留档）
    runtime_dir = os.environ.get("HARNESS_RUNTIME_DIR") or os.path.join(
        harness_root, "runtime")
    backend = (os.environ.get("HARNESS_STORE_BACKEND") or "sqlite").lower()
    if backend == "json":
        return LocalJsonStore(runtime_dir)
    return SqliteStore(runtime_dir)


def new_transition(task_id, from_version, to_version, event_type,
                   transition_type, changes=None, reason_code=""):
    return {
        "transition_id": "TR-%s-%s" % (time.strftime("%Y%m%d%H%M%S"), os.urandom(2).hex()),
        "task_id": task_id,
        "from_version": from_version,
        "to_version": to_version,
        "event": {"type": event_type},
        "transition_type": transition_type,
        "changes": changes or {},
        "reason_code": reason_code,
        "created_at": _now(),
    }
