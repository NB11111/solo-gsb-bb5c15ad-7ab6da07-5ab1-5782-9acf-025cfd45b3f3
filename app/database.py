"""SQLite 持久化层：保存每次审查的文档、结论与发现。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# 容器内默认落在挂载卷 /data；本地开发默认项目下 data/
DB_PATH = os.environ.get("REVIEW_DB", "data/reviews.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    title        TEXT,
    old_filename TEXT,
    new_filename TEXT,
    old_version  TEXT,
    new_version  TEXT,
    summary      TEXT    NOT NULL,
    findings     TEXT    NOT NULL,
    old_spec     TEXT    NOT NULL,
    new_spec     TEXT    NOT NULL
);
"""


def init_db() -> None:
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(_SCHEMA)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_review(
    *,
    title: str | None,
    old_filename: str | None,
    new_filename: str | None,
    old_version: str | None,
    new_version: str | None,
    summary: dict[str, Any],
    findings: list[dict[str, Any]],
    old_doc: dict[str, Any],
    new_doc: dict[str, Any],
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO reviews
                (title, old_filename, new_filename, old_version, new_version,
                 summary, findings, old_spec, new_spec)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                old_filename,
                new_filename,
                old_version,
                new_version,
                json.dumps(summary, ensure_ascii=False),
                json.dumps(findings, ensure_ascii=False),
                json.dumps(old_doc, ensure_ascii=False),
                json.dumps(new_doc, ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)


def _row_to_brief(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "title": row["title"],
        "old_filename": row["old_filename"],
        "new_filename": row["new_filename"],
        "old_version": row["old_version"],
        "new_version": row["new_version"],
        "summary": json.loads(row["summary"]),
    }


def list_reviews() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reviews ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return [_row_to_brief(r) for r in rows]


def get_review(review_id: int, *, with_specs: bool = False) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM reviews WHERE id = ?", (review_id,)
        ).fetchone()
    if row is None:
        return None
    data = _row_to_brief(row)
    data["findings"] = json.loads(row["findings"])
    if with_specs:
        data["old_spec"] = json.loads(row["old_spec"])
        data["new_spec"] = json.loads(row["new_spec"])
    return data


def get_specs(review_id: int) -> tuple[dict[str, Any], dict[str, Any]] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT old_spec, new_spec FROM reviews WHERE id = ?", (review_id,)
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["old_spec"]), json.loads(row["new_spec"])


def delete_review(review_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
        return cur.rowcount > 0
