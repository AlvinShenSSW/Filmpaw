"""Append-only operation receipts; filesystem effects are never inferred.

An intent commits before a mutation and a receipt follows its successful return.
An interruption between them is explicitly uncertain. History is not a command
queue and recovery never invokes filesystem actions.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone


class AuditError(RuntimeError):
    """Persistent operation history failed; callers must stop further writes."""


OUTCOMES = {"started", "succeeded", "failed", "skipped", "stopped", "needs_review"}


class OperationLog:
    def __init__(self, conn: sqlite3.Connection, db_lock):
        self.conn = conn
        self.db_lock = db_lock
        self.failed = False

    def record(self, task: dict, action: str, outcome: str, *, item_name: str | None = None,
               source_path: str | None = None, target_path: str | None = None,
               operation_id: str | None = None, message: str | None = None,
               kind: str = "transfer") -> dict:
        if outcome not in OUTCOMES or kind not in {"transfer", "cleanup"}:
            raise ValueError("invalid operation event")
        source_path = source_path if source_path is not None else task.get("source_path", task.get("batch_path"))
        target_path = target_path if target_path is not None else task.get("target_path")
        if operation_id is None:
            identity = json.dumps([task["id"], kind, action, item_name, source_path, target_path], ensure_ascii=False)
            operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
        event_key = f"{operation_id}:{outcome}"
        with self.db_lock:
            if self.failed:
                raise AuditError("操作日志无法保存，已停止后续操作；请重启后核对结果")
            try:
                self.conn.execute(
                    "INSERT INTO operation_events(event_key,operation_id,created_at,task_id,kind,action,outcome,"
                    "performer_name,item_name,source_path,target_path,message) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(event_key) DO NOTHING",
                    (event_key, operation_id, datetime.now(timezone.utc).isoformat(), task["id"], kind,
                     action, outcome, task.get("performer_name"), item_name, source_path, target_path, message),
                )
                row = self.conn.execute("SELECT * FROM operation_events WHERE event_key=?", (event_key,)).fetchone()
                self.conn.commit()
                return dict(row)
            except Exception as exc:
                self.failed = True
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                raise AuditError(f"操作日志保存失败，后续操作已停止：{exc}") from exc

    def perform(self, task: dict, action: str, fn, **fields):
        self.record(task, action, "started", **fields)
        try:
            result = fn()
        except AuditError:
            raise
        except Exception as exc:
            self.record(task, action, "failed", **{**fields, "message": str(exc) or type(exc).__name__})
            raise
        self.record(task, action, "succeeded", **fields)
        return result

    def list(self, *, before_id: int | None = None, limit: int = 50,
             task_id: str | None = None, action: str | None = None,
             outcome: str | None = None) -> dict:
        if not 1 <= limit <= 200 or (before_id is not None and before_id < 1):
            raise ValueError("invalid history page")
        clauses, params = [], []
        for column, value in [("task_id", task_id), ("action", action), ("outcome", outcome)]:
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if before_id is not None:
            clauses.append("id<?")
            params.append(before_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db_lock:
            rows = self.conn.execute(
                "SELECT * FROM operation_events" + where + " ORDER BY id DESC LIMIT ?", [*params, limit + 1],
            ).fetchall()
        items = [dict(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    def recover_pending(self) -> None:
        """Close previous unconfirmed intents as uncertain, not as successful."""
        with self.db_lock:
            rows = self.conn.execute(
                "SELECT started.* FROM operation_events started WHERE started.outcome='started'"
                " AND NOT EXISTS (SELECT 1 FROM operation_events result"
                " WHERE result.operation_id=started.operation_id AND result.outcome!='started') ORDER BY started.id",
            ).fetchall()
        for row in rows:
            self.record(
                {"id": row["task_id"], "performer_name": row["performer_name"]}, row["action"], "needs_review",
                kind=row["kind"], operation_id=row["operation_id"], item_name=row["item_name"],
                source_path=row["source_path"], target_path=row["target_path"],
                message="上次操作的最终结果未能确认，请人工核对；未自动重试或删除",
            )
