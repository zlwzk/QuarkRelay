"""历史记录存储（SQLite）：转存/分享/搬运的流水，可搜索、可导出。"""

from __future__ import annotations

import csv
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import HISTORY_FILE, ensure_dirs

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,           -- transfer / share / relay
    name        TEXT NOT NULL DEFAULT '',
    link        TEXT NOT NULL DEFAULT '',
    code        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',-- quark / baidu
    target_path TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_created ON records(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_records_link ON records(link);
"""


@dataclass(slots=True)
class Record:
    id: int
    kind: str
    name: str
    link: str
    code: str
    source: str
    target_path: str
    note: str
    created_at: float

    @property
    def combined(self) -> str:
        code = f" 提取码：{self.code}" if self.code else ""
        return f"{self.name} {self.link}{code}".strip()


class HistoryStore:
    def __init__(self, path: Path | None = None) -> None:
        ensure_dirs()
        self._path = path or HISTORY_FILE
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ api
    def add(
        self,
        kind: str,
        name: str = "",
        link: str = "",
        code: str = "",
        source: str = "",
        target_path: str = "",
        note: str = "",
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO records(kind,name,link,code,source,target_path,note,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (kind, name, link, code, source, target_path, note, time.time()),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def list(self, keyword: str = "", limit: int = 300) -> list[Record]:
        sql = "SELECT * FROM records"
        params: list[object] = []
        if keyword:
            sql += " WHERE name LIKE ? OR link LIKE ? OR note LIKE ?"
            like = f"%{keyword}%"
            params += [like, like, like]
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Record(**dict(row)) for row in rows]

    def delete(self, record_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM records WHERE id=?", (record_id,))
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM records")
            self._conn.commit()

    def trim(self, keep: int) -> None:
        if keep <= 0:
            return
        with self._lock:
            self._conn.execute(
                "DELETE FROM records WHERE id NOT IN"
                " (SELECT id FROM records ORDER BY created_at DESC LIMIT ?)",
                (keep,),
            )
            self._conn.commit()

    def export_csv(self, path: str | Path, keyword: str = "") -> int:
        rows = self.list(keyword=keyword, limit=10_000)
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["时间", "类型", "名字", "链接", "提取码", "来源", "目录", "备注", "名字+链接"])
            for row in rows:
                writer.writerow(
                    [
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row.created_at)),
                        row.kind,
                        row.name,
                        row.link,
                        row.code,
                        row.source,
                        row.target_path,
                        row.note,
                        row.combined,
                    ]
                )
        return len(rows)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


_store: HistoryStore | None = None


def history() -> HistoryStore:
    global _store
    if _store is None:
        _store = HistoryStore()
    return _store
