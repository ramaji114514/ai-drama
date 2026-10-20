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
    synced      TEXT,
    job_ids     TEXT,
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
    simulated   INTEGER NOT NULL DEFAULT 0,
    shot_label  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_costs_run ON costs(run_id);

CREATE TABLE IF NOT EXISTS assets (
    asset_id    TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL DEFAULT '',
    stage       TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'other',
    rel         TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    shot_id     TEXT NOT NULL DEFAULT '',
    prompt_hash TEXT NOT NULL DEFAULT '',
    prompt      TEXT NOT NULL DEFAULT '',
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assets_run ON assets(run_id);
CREATE INDEX IF NOT EXISTS idx_assets_sha ON assets(sha256);
CREATE INDEX IF NOT EXISTS idx_assets_kind ON assets(run_id, kind);

-- 血缘：parent -> child。rel 是 ref / source / derive（见 core/assets.py）。
CREATE TABLE IF NOT EXISTS asset_edges (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    rel        TEXT NOT NULL DEFAULT 'derive',
    created_at TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id, rel)
);
CREATE INDEX IF NOT EXISTS idx_edges_child ON asset_edges(child_id);
"""


class Store:
    """线程安全的 SQLite 封装（视频/关键帧阶段会并发调用）。"""

    #: 后加的列。老库（已经跑过几次的 runs/ai_drama.sqlite3）用 ALTER TABLE 补上 ——
    #: 否则升级后第一件事就是 `no such column: synced`，而且是在跑到一半的时候炸。
    #: 只做"加列"这一种迁移：可逆、不丢数据。改类型、删列必须显式写迁移脚本，
    #: 不能在这里偷偷做。
    _ADDED_COLUMNS = (
        ("shots", "synced", "TEXT"),
        ("shots", "job_ids", "TEXT"),
        ("costs", "simulated", "INTEGER NOT NULL DEFAULT 0"),
    )

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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """把老库补齐到当前 schema。调用方已持有锁。"""
        for table, column, decl in self._ADDED_COLUMNS:
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        # 老账单里的 provider 名还留着，占位实现一眼能认出来（registry 统一叫
        # `dryrun-<kind>`）。所以这不是猜，是把一条本来就算得出来的事实补上 ——
        # 不补的话，升级后打开老 run，那 0.18 美元会被当成真花的钱。
        # 幂等：只碰还没标过的行，正常启动时扫描到 0 行。
        self._conn.execute(
            "UPDATE costs SET simulated=1 WHERE simulated=0 AND provider LIKE 'dryrun%'"
        )

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
        # job_ids 是 dict（按拆段序号存），落库前转 JSON。放在这里而不是让每个
        # 调用方自己 dumps，是因为漏一次就是"job_id 又只活在内存里" ——
        # 那正是这个字段存在的意义。
        if isinstance(fields.get("job_ids"), (dict, list)):
            fields["job_ids"] = json.dumps(fields["job_ids"], ensure_ascii=False)
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE shots SET {cols} WHERE shot_id=?", (*fields.values(), shot_id))

    def shot_jobs(self, shot_id: str) -> Dict[str, str]:
        """这个镜头已提交但还没结算的任务（{"1": "job-abc"}）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT job_ids FROM shots WHERE shot_id=?", (shot_id,)
            ).fetchone()
        if not row or not row["job_ids"]:
            return {}
        try:
            data = json.loads(row["job_ids"])
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def pending_shots(self, run_id: str, stage_field: str) -> List[Dict[str, Any]]:
        """返回某个阶段还需要处理的镜头。

        stage_field 只能是 keyframe / video / audio / synced — 白名单校验，
        避免 SQL 注入。
        """
        if stage_field not in ("keyframe", "video", "audio", "synced"):
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
                " simulated, shot_label, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    entry.run_id, entry.provider, entry.stage, entry.unit,
                    entry.amount, entry.money, 1 if entry.simulated else 0,
                    entry.shot_label, entry.created_at,
                ),
            )

    def cost_summary(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT provider, stage, unit, SUM(amount) AS amount, SUM(money) AS money,"
                " MAX(simulated) AS simulated"
                " FROM costs WHERE run_id=? GROUP BY provider, stage, unit"
                " ORDER BY money DESC",
                (run_id,),
            ).fetchall()
            total = self._conn.execute(
                "SELECT COALESCE(SUM(money),0) AS m,"
                " COALESCE(SUM(CASE WHEN simulated THEN money ELSE 0 END),0) AS sim"
                " FROM costs WHERE run_id=?",
                (run_id,),
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
                "simulated": bool(r["simulated"]),
            }
            for r in rows
        ]
        # 按阶段 / 按 provider 的横向汇总。做成本优化时你要看的就是这两个视图：
        # "钱花在哪一步" 和 "钱花在哪家"。
        by_stage: Dict[str, float] = {}
        by_provider: Dict[str, float] = {}
        #: 上面两张表的"其中多少是占位估算"。键集合与真扣费那张一致，
        #: 差值就是真花的钱 —— 展示层不用自己再减一遍。
        by_stage_simulated: Dict[str, float] = {}
        by_provider_simulated: Dict[str, float] = {}
        for line in lines:
            if not line["money"]:
                continue
            by_stage[line["stage"]] = round(by_stage.get(line["stage"], 0.0) + line["money"], 6)
            by_provider[line["provider"]] = round(
                by_provider.get(line["provider"], 0.0) + line["money"], 6
            )
            if line["simulated"]:
                by_stage_simulated[line["stage"]] = round(
                    by_stage_simulated.get(line["stage"], 0.0) + line["money"], 6
                )
                by_provider_simulated[line["provider"]] = round(
                    by_provider_simulated.get(line["provider"], 0.0) + line["money"], 6
                )
        simulated_money = round(float(total["sim"]), 6)
        return {
            "run_id": run_id,
            "total_money": round(float(total["m"]), 6),
            "simulated_money": simulated_money,
            "real_money": round(float(total["m"]) - simulated_money, 6),
            "calls": int(calls["n"]),
            "by_stage": dict(sorted(by_stage.items(), key=lambda kv: -kv[1])),
            "by_provider": dict(sorted(by_provider.items(), key=lambda kv: -kv[1])),
            "by_stage_simulated": by_stage_simulated,
            "by_provider_simulated": by_provider_simulated,
            "lines": lines,
        }

    # ————————————————————————————————————- assets（资产台账 + 血缘）

    def upsert_asset(self, asset: Any) -> None:
        """写一条资产记录。**同 id 覆盖**，不追加。

        id 由 (run_id, 相对路径) 推导，所以一个阶段重跑时是同一行被改写 ——
        追加的话，"这件东西生成过几次"这个问题在台账里就永远答不对了。
        """
        row = asset.to_row()
        now = now_iso()
        with self.tx() as c:
            c.execute(
                "INSERT INTO assets (asset_id, run_id, stage, kind, rel, size, sha256,"
                " provider, model, shot_id, prompt_hash, prompt, meta, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(asset_id) DO UPDATE SET"
                " stage=excluded.stage, kind=excluded.kind, rel=excluded.rel,"
                " size=excluded.size, sha256=excluded.sha256, provider=excluded.provider,"
                " model=excluded.model, shot_id=excluded.shot_id,"
                " prompt_hash=excluded.prompt_hash, prompt=excluded.prompt,"
                " meta=excluded.meta, updated_at=excluded.updated_at",
                (
                    row["asset_id"], row["run_id"], row["stage"], row["kind"], row["rel"],
                    row["size"], row["sha256"], row["provider"], row["model"],
                    row["shot_id"], row["prompt_hash"], row["prompt"], row["meta"],
                    now, now,
                ),
            )

    def link_assets(self, parent_id: str, child_id: str, rel: str = "derive") -> None:
        """记一条血缘边。同一对 (parent, child, rel) 天然幂等。"""
        if not parent_id or not child_id or parent_id == child_id:
            return
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO asset_edges (parent_id, child_id, rel, created_at)"
                " VALUES (?,?,?,?)",
                (parent_id, child_id, rel, now_iso()),
            )

    def get_asset(self, asset_id: str) -> Optional[Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
        return _asset(row) if row else None

    def list_assets(self, run_id: str = "", *, kind: str = "",
                    limit: int = 0) -> List[Any]:
        sql = "SELECT * FROM assets WHERE 1=1"
        args: List[Any] = []
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY run_id, kind, rel"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_asset(r) for r in rows]

    def assets_sharing_content(self, sha256: str, *, exclude: str = "") -> List[Any]:
        """sha256 相同的记录 —— 用来回答"这张图在别处也出现过吗"。"""
        if not sha256:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM assets WHERE sha256=? AND asset_id<>?",
                (sha256, exclude),
            ).fetchall()
        return [_asset(r) for r in rows]

    def asset_edges(self, asset_id_: str = "") -> List[Any]:
        """给定 id 时返回**与它有关**的两个方向的边；不给则返回全部。"""
        with self._lock:
            if asset_id_:
                rows = self._conn.execute(
                    "SELECT * FROM asset_edges WHERE parent_id=? OR child_id=?",
                    (asset_id_, asset_id_),
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM asset_edges").fetchall()
        return [dict(r) for r in rows]

    def asset_summary(self, run_id: str) -> Dict[str, Any]:
        """按种类点数 + 总字节数。刻意不打钱 —— 钱在 costs 表里，两笔账分开算。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n, COALESCE(SUM(size),0) AS b"
                " FROM assets WHERE run_id=? GROUP BY kind", (run_id,)
            ).fetchall()
            edges = self._conn.execute(
                "SELECT COUNT(*) AS n FROM asset_edges e JOIN assets a ON a.asset_id=e.child_id"
                " WHERE a.run_id=?", (run_id,)
            ).fetchone()
        by_kind = {r["kind"]: {"count": int(r["n"]), "bytes": int(r["b"])} for r in rows}
        return {
            "run_id": run_id,
            "total": sum(v["count"] for v in by_kind.values()),
            "bytes": sum(v["bytes"] for v in by_kind.values()),
            "edges": int(edges["n"]) if edges else 0,
            "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1]["count"])),
        }


def _asset(row: Any) -> Any:
    from .assets import Asset

    return Asset.from_row(row)


def default_db_path() -> Path:
    env = os.environ.get("AI_DRAMA_DB")
    if env:
        return Path(env)
    return Path.cwd() / "runs" / "ai_drama.sqlite3"
