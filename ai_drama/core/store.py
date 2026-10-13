"""SQLite 状态存储 + 成本记账。

这是整个骨架与"能跑的 demo"之间最大的区别：**每一步都落盘**。

  1. 每个 shot 的 state 都写进 SQLite，进程被杀掉之后
     `ai-drama resume <run_id>` 能从断点继续，已完成的镜头直接跳过。
  2. 每次 provider 调用都记一条 cost，跑完能拿到精确账单。
  3. 中间产物（分镜 JSON、每镜的图/视频/音频路径）全部持久化。

刻意用 sqlite3 而不是 ORM：零依赖，且这份 schema 直接就是
你以后换成 Postgres 时的迁移起点。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .models import CostEntry, Episode, Run, Shot, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    idea        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',
    stage       TEXT NOT NULL DEFAULT 'script',
    out_dir     TEXT NOT NULL DEFAULT '',
    final_video TEXT,
    error       TEXT,
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episode (
    run_id   TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    payload  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shots (
    shot_id     TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    scene_id    TEXT NOT NULL,
    ord         INTEGER NOT NULL,
    label       TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',
    duration    REAL NOT NULL DEFAULT 0,
    payload     TEXT NOT NULL,
    keyframe    TEXT,
    video       TEXT,
    audio       TEXT,
    error       TEXT,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shots_run ON shots(run_id, scene_id, ord);

CREATE TABLE IF NOT EXISTS costs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL,
    stage       TEXT NOT NULL,
    unit        TEXT NOT NULL,
    amount      REAL NOT NULL,
    money       REAL NOT NULL,
    shot_label  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_costs_run ON costs(run_id);
"""


class Store:
    """线程安全的 SQLite 封装（视频/关键帧阶段会并发调用）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ————————————————————————————————————- runs

    def create_run(self, run: Run) -> Run:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO runs (run_id, idea, state, stage, out_dir,"
                " final_video, error, meta, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run.run_id, run.idea, run.state, run.stage, run.out_dir,
                    run.final_video, run.error, json.dumps(run.meta, ensure_ascii=False),
                    run.created_at, run.updated_at,
                ),
            )
        return run

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "meta" in fields and not isinstance(fields["meta"], str):
            fields["meta"] = json.dumps(fields["meta"], ensure_ascii=False)
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE runs SET {cols} WHERE run_id=?", (*fields.values(), run_id))

    def get_run(self, run_id: str) -> Optional[Run]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["meta"] = json.loads(d.get("meta") or "{}")
        return Run(**d)

    def list_runs(self, limit: int = 20) -> List[Run]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["meta"] = json.loads(d.get("meta") or "{}")
            out.append(Run(**d))
        return out

    # ————————————————————————————————----- episode

    def save_episode(self, ep: Episode) -> None:
        payload = json.dumps(ep.to_dict(), ensure_ascii=False)
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO episode (run_id, payload) VALUES (?,?)",
                (ep.run_id, payload),
            )

    def get_episode(self, run_id: str) -> Optional[Episode]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM episode WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row:
            return None
        return Episode.from_dict(json.loads(row["payload"]))

    # ———————————————————————————————————— shots

    def sync_shots(self, run_id: str, ep: Episode) -> None:
        """把分镜表写进 shots 表。已存在的行保留其 state / 产物路径。

        这是断点续跑的关键：重跑分镜阶段不会把已经渲染好的镜头标记回 pending。
        """
        with self.tx() as c:
            for shot in ep.shots:
                exists = c.execute(
                    "SELECT state, keyframe, video, audio FROM shots WHERE shot_id=?",
                    (shot.shot_id,),
                ).fetchone()
                if exists:
                    # 只更新剧本层面的字段，保留运行时状态
                    c.execute(
                        "UPDATE shots SET scene_id=?, ord=?, label=?, duration=?,"
                        " payload=?, updated_at=? WHERE shot_id=?",
                        (
                            shot.scene_id, shot.order, shot.label, shot.duration,
                            json.dumps(shot.to_dict(), ensure_ascii=False),
                            now_iso(), shot.shot_id,
                        ),
                    )
                else:
                    c.execute(
                        "INSERT INTO shots (shot_id, run_id, scene_id, ord, label,"
                        " state, duration, payload, updated_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            shot.shot_id, run_id, shot.scene_id, shot.order, shot.label,
                            shot.state, shot.duration,
                            json.dumps(shot.to_dict(), ensure_ascii=False), now_iso(),
                        ),
                    )

    def list_shots(self, run_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM shots WHERE run_id=? ORDER BY scene_id, ord", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_shot(self, shot_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE shots SET {cols} WHERE shot_id=?", (*fields.values(), shot_id))

    def pending_shots(self, run_id: str, stage_field: str) -> List[Dict[str, Any]]:
        """返回某个阶段还需要处理的镜头。

        stage_field 只能是 keyframe / video / audio — 白名单校验，避免 SQL 注入。
        """
        if stage_field not in ("keyframe", "video", "audio"):
            raise ValueError(f"bad stage_field: {stage_field}")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM shots WHERE run_id=? AND ({stage_field} IS NULL"
                f" OR {stage_field}='') ORDER BY scene_id, ord",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ———————————————————————————————————— costs

    def add_cost(self, entry: CostEntry) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO costs (run_id, provider, stage, unit, amount, money,"
                " shot_label, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    entry.run_id, entry.provider, entry.stage, entry.unit,
                    entry.amount, entry.money, entry.shot_label, entry.created_at,
                ),
            )

    def cost_summary(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT provider, stage, unit, SUM(amount) AS amount, SUM(money) AS money"
                " FROM costs WHERE run_id=? GROUP BY provider, stage, unit"
                " ORDER BY money DESC",
                (run_id,),
            ).fetchall()
            total = self._conn.execute(
                "SELECT COALESCE(SUM(money),0) AS m FROM costs WHERE run_id=?", (run_id,)
            ).fetchone()
            calls = self._conn.execute(
                "SELECT COUNT(*) AS n FROM costs WHERE run_id=?", (run_id,)
            ).fetchone()
        lines = [
            {
                "provider": r["provider"],
                "stage": r["stage"],
                "unit": r["unit"],
                "amount": round(float(r["amount"] or 0), 4),
                "money": round(float(r["money"] or 0), 6),
            }
            for r in rows
        ]
        # 按阶段 / 按 provider 的横向汇总。做成本优化时你要看的就是这两个视图：
        # "钱花在哪一步" 和 "钱花在哪家"。
        by_stage: Dict[str, float] = {}
        by_provider: Dict[str, float] = {}
        for line in lines:
            by_stage[line["stage"]] = round(by_stage.get(line["stage"], 0.0) + line["money"], 6)
            by_provider[line["provider"]] = round(
                by_provider.get(line["provider"], 0.0) + line["money"], 6
            )
        return {
            "run_id": run_id,
            "total_money": round(float(total["m"]), 6),
            "calls": int(calls["n"]),
            "by_stage": dict(sorted(by_stage.items(), key=lambda kv: -kv[1])),
            "by_provider": dict(sorted(by_provider.items(), key=lambda kv: -kv[1])),
            "lines": lines,
        }


def default_db_path() -> Path:
    env = os.environ.get("AI_DRAMA_DB")
    if env:
        return Path(env)
    return Path.cwd() / "runs" / "ai_drama.sqlite3"
