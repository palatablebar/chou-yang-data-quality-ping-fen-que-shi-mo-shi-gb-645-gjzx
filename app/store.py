"""SQLite 存储：数据集原文、运行记录与完整结果快照。

约定：
- 数据集按名字唯一：同名文件重新采信即覆盖原文，历史运行仍按 digest 保留；
- 运行记录按 (dataset, digest, rules_version) 幂等：同输入重跑不新增行；
- 分数与比率落 TEXT（Decimal 字符串），不落 float；
- 页面永远只展示一条运行记录，导出读的是同一条快照。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    name TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    encoding TEXT NOT NULL,
    declared_encoding TEXT,
    bytes INTEGER NOT NULL,
    content BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    digest TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    version INTEGER NOT NULL,
    score TEXT NOT NULL,
    grade TEXT NOT NULL,
    rows_in INTEGER NOT NULL,
    rows_accepted INTEGER NOT NULL,
    rows_unique INTEGER NOT NULL,
    rows_dup INTEGER NOT NULL,
    rows_quarantine INTEGER NOT NULL,
    rows_blank INTEGER NOT NULL,
    cells_total INTEGER NOT NULL,
    cells_missing INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_seq INTEGER NOT NULL,
    summary_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_identity ON runs(name, digest, rules_version);
CREATE INDEX IF NOT EXISTS idx_runs_version ON runs(name, version DESC);

CREATE TABLE IF NOT EXISTS return_batches (
    batch TEXT PRIMARY KEY,
    active_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS return_versions (
    batch TEXT NOT NULL,
    version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    roster_digest TEXT NOT NULL,
    attachment_digest TEXT NOT NULL,
    roster_text TEXT NOT NULL,
    attachment BLOB NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    seen_seq INTEGER NOT NULL,
    PRIMARY KEY (batch, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_return_identity
    ON return_versions(batch, digest);
CREATE INDEX IF NOT EXISTS idx_return_version
    ON return_versions(batch, version DESC);
CREATE TABLE IF NOT EXISTS return_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch TEXT NOT NULL,
    digest TEXT NOT NULL,
    outcome TEXT NOT NULL,
    version INTEGER,
    detail TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 1,
    first_at TEXT NOT NULL,
    last_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_return_attempt
    ON return_attempts(batch, digest, outcome);
CREATE INDEX IF NOT EXISTS idx_return_attempt_batch
    ON return_attempts(batch, last_at DESC);
"""


def _dec(text):
    return None if text is None else Decimal(str(text))


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 数据集 ----------
    def save_dataset(self, *, name: str, digest: str, encoding: str,
                     declared_encoding: str | None, data: bytes, stamp: str) -> None:
        """存原始字节：GB18030 文件重算摘要必须与首次采信逐位相同。"""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO datasets (name,digest,encoding,declared_encoding,bytes,
                        content,updated_at) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     digest=excluded.digest, encoding=excluded.encoding,
                     declared_encoding=excluded.declared_encoding,
                     bytes=excluded.bytes, content=excluded.content,
                     updated_at=excluded.updated_at""",
                (name, digest, encoding, declared_encoding, len(data), data, stamp))

    def get_dataset(self, name: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM datasets WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def list_datasets(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT name,digest,encoding,bytes,updated_at FROM datasets ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 运行记录 ----------
    def latest_version(self, name: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM runs WHERE name=?", (name,)).fetchone()
        return int(row["v"] or 0)

    def find_run(self, name: str, digest: str, rules_version: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE name=? AND digest=? AND rules_version=?",
                (name, digest, rules_version)).fetchone()
        return dict(row) if row else None

    def save_run(self, run: dict, summary: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO runs (run_id,name,digest,rules_version,version,score,grade,
                        rows_in,rows_accepted,rows_unique,rows_dup,rows_quarantine,
                        rows_blank,cells_total,cells_missing,created_at,finished_at,
                        last_seen_at,seen_seq,summary_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run["run_id"], run["name"], run["digest"], run["rules_version"],
                 int(run["version"]), str(run["score"]), run["grade"],
                 int(run["rows_in"]), int(run["rows_accepted"]), int(run["rows_unique"]),
                 int(run["rows_dup"]), int(run["rows_quarantine"]),
                 int(run["rows_blank"]), int(run["cells_total"]),
                 int(run["cells_missing"]), run["created_at"], run["finished_at"],
                 run["created_at"], self._next_seq(conn),
                 json.dumps(summary, ensure_ascii=False)))

    def touch_run(self, run_id: str, stamp: str) -> None:
        """命中幂等复用时刷新「最近一次查看」，导航回各页仍停在这条记录上。"""
        with self.connect() as conn:
            conn.execute("UPDATE runs SET last_seen_at=?, seen_seq=? WHERE run_id=?",
                         (stamp, self._next_seq(conn), run_id))

    def _next_seq(self, conn) -> int:
        row = conn.execute("SELECT COALESCE(MAX(seen_seq), 0) + 1 AS n FROM runs").fetchone()
        return int(row["n"])

    def latest_run(self, name: str | None = None) -> dict | None:
        with self.connect() as conn:
            if name:
                row = conn.execute(
                    "SELECT * FROM runs WHERE name=? ORDER BY version DESC LIMIT 1",
                    (name,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM runs ORDER BY seen_seq DESC LIMIT 1"
                ).fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY seen_seq DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(row) for row in rows]

    def load_result(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT summary_json FROM runs WHERE run_id=?",
                               (run_id,)).fetchone()
        return json.loads(row["summary_json"]) if row else None

    def count_runs(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"])

    # ---------- 回传批次 ----------
    def active_return_version(self, batch: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT active_version FROM return_batches WHERE batch=?",
                (batch,)).fetchone()
        return int(row["active_version"]) if row else 0

    def find_return_version(self, batch: str, digest: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT batch,version,digest,roster_digest,attachment_digest,
                          created_at,updated_at,seen_seq
                   FROM return_versions WHERE batch=? AND digest=?""",
                (batch, digest)).fetchone()
        return dict(row) if row else None

    def get_return_version(self, batch: str, version: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT batch,version,digest,roster_digest,attachment_digest,
                          created_at,updated_at,seen_seq
                   FROM return_versions WHERE batch=? AND version=?""",
                (batch, version)).fetchone()
        return dict(row) if row else None

    def active_return_record(self, batch: str) -> dict | None:
        """当前生效版本：替换失败回滚后，指针可能低于已写下的最高版本。"""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT v.batch,v.version,v.digest,v.roster_digest,
                          v.attachment_digest,v.created_at,v.updated_at,v.seen_seq
                   FROM return_versions v
                   JOIN return_batches b
                     ON b.batch=v.batch AND b.active_version=v.version
                   WHERE v.batch=?""",
                (batch,)).fetchone()
        return dict(row) if row else None

    def active_return_record_any(self) -> dict | None:
        """跨批次取最近查看的生效版本。"""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT v.batch,v.version,v.digest,v.roster_digest,
                          v.attachment_digest,v.created_at,v.updated_at,v.seen_seq
                   FROM return_versions v
                   JOIN return_batches b
                     ON b.batch=v.batch AND b.active_version=v.version
                   ORDER BY v.seen_seq DESC, v.updated_at DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def max_return_version(self, batch: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM return_versions WHERE batch=?",
                (batch,)).fetchone()
        return int(row["v"] or 0)

    def delete_return_version(self, batch: str, version: int) -> None:
        """只删未生效的失败快照；生效版本的原文与结果一律不删。"""
        with self.connect() as conn:
            conn.execute(
                """DELETE FROM return_versions
                   WHERE batch=? AND version=?
                     AND version NOT IN (SELECT active_version FROM return_batches
                                         WHERE batch=?)""",
                (batch, int(version), batch))

    def save_return_version(self, *, batch: str, version: int, digest: str,
                            roster_digest: str, attachment_digest: str,
                            roster_text: str, attachment: bytes,
                            result: dict, stamp: str) -> None:
        """一次新版本：结果快照与两份输入原文同事务写入。

        版本号由调用方在写锁内取到；同批次同版本只会写一次。写入不等于生效，
        指针切换由 activate_return_version 单独完成。
        """
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO return_versions (batch,version,digest,roster_digest,
                        attachment_digest,roster_text,attachment,result_json,
                        created_at,updated_at,seen_seq)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (batch, int(version), digest, roster_digest, attachment_digest,
                 roster_text, attachment,
                 json.dumps(result, ensure_ascii=False), stamp, stamp,
                 self._next_seq(conn)))

    def activate_return_version(self, batch: str, version: int, stamp: str) -> None:
        """切换生效指针；只有快照已完整写入的版本才会被指到。"""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO return_batches (batch,active_version,updated_at)
                   VALUES (?,?,?)
                   ON CONFLICT(batch) DO UPDATE SET
                     active_version=excluded.active_version,
                     updated_at=excluded.updated_at""",
                (batch, int(version), stamp))

    def rollback_return_version(self, batch: str, attempted: int, fallback: int,
                                stamp: str) -> None:
        """只在指针仍指向本次尝试的版本时回退，避免把别处刚切好的新版本顶掉。"""
        with self.connect() as conn:
            conn.execute(
                """UPDATE return_batches SET active_version=?, updated_at=?
                   WHERE batch=? AND active_version=?""",
                (int(fallback), stamp, batch, int(attempted)))

    def touch_return_version(self, batch: str, version: int, stamp: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE return_versions SET updated_at=?, seen_seq=?"
                " WHERE batch=? AND version=?",
                (stamp, self._next_seq(conn), batch, int(version)))

    def load_return_result(self, batch: str, version: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM return_versions WHERE batch=? AND version=?",
                (batch, int(version))).fetchone()
        return json.loads(row["result_json"]) if row else None

    def load_return_inputs(self, batch: str, version: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT roster_text, attachment FROM return_versions
                   WHERE batch=? AND version=?""",
                (batch, int(version))).fetchone()
        if row is None:
            return None
        return {"roster_text": row["roster_text"], "attachment": row["attachment"]}

    def list_return_versions(self, batch: str, limit: int = 12) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT batch,version,digest,roster_digest,attachment_digest,
                          created_at,updated_at
                   FROM return_versions WHERE batch=? ORDER BY version DESC LIMIT ?""",
                (batch, limit)).fetchall()
        return [dict(row) for row in rows]

    def record_return_attempt(self, *, batch: str, digest: str, outcome: str,
                              version: int | None, detail: dict,
                              stamp: str) -> dict:
        """同批次同输入同结果的尝试只累加 hits，不新增行，也不丢记录。

        返回该尝试的最新累计次数；替换失败与成功分开计（outcome 不同），
        失败一次后重试成功会同时留下两条，便于说明「重试不重复计数」。
        """
        payload = json.dumps(detail, ensure_ascii=False)
        with self.connect() as conn:
            row = conn.execute(
                """SELECT id,hits FROM return_attempts
                   WHERE batch=? AND digest=? AND outcome=?""",
                (batch, digest, outcome)).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO return_attempts (batch,digest,outcome,version,
                            detail,hits,first_at,last_at)
                       VALUES (?,?,?,?,?,1,?,?)""",
                    (batch, digest, outcome,
                     None if version is None else int(version), payload, stamp, stamp))
                hits = 1
            else:
                hits = int(row["hits"]) + 1
                conn.execute(
                    """UPDATE return_attempts SET hits=?, last_at=?, version=?,
                            detail=? WHERE id=?""",
                    (hits, stamp, None if version is None else int(version),
                     payload, row["id"]))
        return {"outcome": outcome, "hits": hits}

    def list_return_attempts(self, batch: str, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT batch,digest,outcome,version,detail,hits,first_at,last_at
                   FROM return_attempts WHERE batch=?
                   ORDER BY last_at DESC, id DESC LIMIT ?""",
                (batch, limit)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            items.append(item)
        return items
