"""KudoSpark 荣誉积分结算库。

基于 SQLite 的本地持久化，由上层直接调用。
提供赠分、兑换、查看成员账况三项能力。
"""

from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

MONTHLY_QUOTA = 50          # 每人每 UTC 自然月可赠分数
WINDOW_SECONDS = 3600       # 滚动窗口长度（秒）
WINDOW_MAX_COUNT = 20       # 滚动窗口内最多成功赠分笔数
MIN_GIFT_POINTS = 1
MAX_GIFT_POINTS = 5


@dataclass(frozen=True)
class MemberSeed:
    """初始化成员数据。"""

    member_id: str
    redeemable: int = 0
    lifetime_received: int = 0


@dataclass(frozen=True)
class RewardSeed:
    """初始化奖励数据（固定价格）。"""

    reward_id: str
    price: int


@dataclass
class Result:
    """结算结果。重复请求返回首次结果并沿用原结算时间。"""

    ok: bool
    request_id: str
    settled_at: Optional[datetime] = None
    error: Optional[str] = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "request_id": self.request_id,
            "settled_at": self.settled_at.isoformat() if self.settled_at else None,
            "error": self.error,
            "detail": dict(self.detail),
        }


class KudoSpark:
    """荣誉积分结算库入口。"""

    def __init__(
        self,
        db_path: str,
        members: Iterable[MemberSeed] = (),
        rewards: Iterable[RewardSeed] = (),
    ) -> None:
        self._conn = sqlite3.connect(db_path)
        self._init_schema()
        self._seed(members, rewards)

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS members (
                    member_id TEXT PRIMARY KEY,
                    redeemable INTEGER NOT NULL,
                    lifetime_received INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rewards (
                    reward_id TEXT PRIMARY KEY,
                    price INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    member_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    op TEXT NOT NULL,
                    content TEXT NOT NULL,
                    result TEXT NOT NULL,
                    settled_at REAL NOT NULL,
                    PRIMARY KEY (member_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS gifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    giver_id TEXT NOT NULL,
                    receiver_id TEXT NOT NULL,
                    points INTEGER NOT NULL,
                    settled_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_gifts_giver_time
                    ON gifts (giver_id, settled_at);
                CREATE TABLE IF NOT EXISTS redemptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    member_id TEXT NOT NULL,
                    reward_id TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    settled_at REAL NOT NULL
                );
                """
            )

    def _seed(
        self, members: Iterable[MemberSeed], rewards: Iterable[RewardSeed]
    ) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO members (member_id, redeemable,"
                " lifetime_received) VALUES (?, ?, ?)",
                [
                    (m.member_id, m.redeemable, m.lifetime_received)
                    for m in members
                ],
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO rewards (reward_id, price) VALUES (?, ?)",
                [(r.reward_id, r.price) for r in rewards],
            )

    @staticmethod
    def _resolve_now(now: Optional[datetime]) -> datetime:
        if now is None:
            return datetime.now(timezone.utc)
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    @staticmethod
    def _ts(dt: datetime) -> float:
        return dt.timestamp()

    @staticmethod
    def _from_ts(ts: float) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    def _member_exists(self, member_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM members WHERE member_id = ?", (member_id,)
        ).fetchone()
        return row is not None

    def _replay(
        self, member_id: str, request_id: str, op: str, content: dict
    ) -> Optional[Result]:
        """命中历史请求时返回其结果；编号冲突时返回拒绝结果。"""
        row = self._conn.execute(
            "SELECT op, content, result, settled_at FROM requests"
            " WHERE member_id = ? AND request_id = ?",
            (member_id, request_id),
        ).fetchone()
        if row is None:
            return None
        stored_op, stored_content, stored_result, settled_at = row
        if stored_op == op and json.loads(stored_content) == content:
            payload = json.loads(stored_result)
            return Result(
                ok=payload["ok"],
                request_id=request_id,
                settled_at=self._from_ts(settled_at),
                error=payload["error"],
                detail=payload["detail"],
            )
        return Result(
            ok=False,
            request_id=request_id,
            settled_at=self._from_ts(settled_at),
            error="request_id_conflict",
        )

    def _record(
        self,
        member_id: str,
        request_id: str,
        op: str,
        content: dict,
        result: Result,
        ts: float,
    ) -> None:
        payload = json.dumps(
            {"ok": result.ok, "error": result.error, "detail": result.detail},
            sort_keys=True,
        )
        self._conn.execute(
            "INSERT INTO requests (member_id, request_id, op, content, result,"
            " settled_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                member_id,
                request_id,
                op,
                json.dumps(content, sort_keys=True),
                payload,
                ts,
            ),
        )

    def give(
        self,
        giver_id: str,
        receiver_id: str,
        points: int,
        request_id: str,
        now: Optional[datetime] = None,
    ) -> Result:
        """赠分。"""
        decided = self._resolve_now(now)
        ts = self._ts(decided)
        content = {"receiver_id": receiver_id, "points": points}
        replayed = self._replay(giver_id, request_id, "give", content)
        if replayed is not None:
            return replayed

        error: Optional[str] = None
        detail: dict = {}
        if not self._member_exists(giver_id):
            error = "unknown_giver"
        elif not self._member_exists(receiver_id):
            error = "unknown_receiver"
        elif giver_id == receiver_id:
            error = "self_gift"
        elif not (MIN_GIFT_POINTS <= points <= MAX_GIFT_POINTS):
            error = "invalid_points"
        else:
            month_key = decided.strftime("%Y-%m")
            used = self._conn.execute(
                "SELECT COALESCE(SUM(points), 0) FROM gifts WHERE giver_id = ?"
                " AND strftime('%Y-%m', settled_at, 'unixepoch') = ?",
                (giver_id, month_key),
            ).fetchone()[0]
            if used + points > MONTHLY_QUOTA:
                error = "monthly_quota_exceeded"
                detail = {"month_used": used, "month_quota": MONTHLY_QUOTA}
            else:
                window_count = self._conn.execute(
                    "SELECT COUNT(*) FROM gifts WHERE giver_id = ?"
                    " AND settled_at > ?",
                    (giver_id, ts - WINDOW_SECONDS),
                ).fetchone()[0]
                if window_count >= WINDOW_MAX_COUNT:
                    error = "window_limit_exceeded"
                    detail = {"window_count": window_count}

        result = Result(
            ok=error is None, request_id=request_id, settled_at=decided,
            error=error, detail=detail,
        )
        with self._conn:
            if error is None:
                self._conn.execute(
                    "INSERT INTO gifts (giver_id, receiver_id, points,"
                    " settled_at) VALUES (?, ?, ?, ?)",
                    (giver_id, receiver_id, points, ts),
                )
                self._conn.execute(
                    "UPDATE members SET redeemable = redeemable + ?,"
                    " lifetime_received = lifetime_received + ?"
                    " WHERE member_id = ?",
                    (points, points, receiver_id),
                )
            self._record(giver_id, request_id, "give", content, result, ts)
        return result

    def redeem(
        self,
        member_id: str,
        reward_id: str,
        request_id: str,
        now: Optional[datetime] = None,
    ) -> Result:
        """兑换奖励。"""
        decided = self._resolve_now(now)
        ts = self._ts(decided)
        content = {"reward_id": reward_id}
        replayed = self._replay(member_id, request_id, "redeem", content)
        if replayed is not None:
            return replayed

        error: Optional[str] = None
        detail: dict = {}
        price: Optional[int] = None
        if not self._member_exists(member_id):
            error = "unknown_member"
        else:
            row = self._conn.execute(
                "SELECT price FROM rewards WHERE reward_id = ?", (reward_id,)
            ).fetchone()
            if row is None:
                error = "unknown_reward"
            else:
                price = row[0]
                balance = self._conn.execute(
                    "SELECT redeemable FROM members WHERE member_id = ?",
                    (member_id,),
                ).fetchone()[0]
                if balance < price:
                    error = "insufficient_balance"
                    detail = {"balance": balance, "price": price}

        result = Result(
            ok=error is None, request_id=request_id, settled_at=decided,
            error=error, detail=detail,
        )
        with self._conn:
            if error is None:
                self._conn.execute(
                    "UPDATE members SET redeemable = redeemable - ?"
                    " WHERE member_id = ?",
                    (price, member_id),
                )
                self._conn.execute(
                    "INSERT INTO redemptions (member_id, reward_id, price,"
                    " settled_at) VALUES (?, ?, ?, ?)",
                    (member_id, reward_id, price, ts),
                )
            self._record(member_id, request_id, "redeem", content, result, ts)
        return result

    def account(self, member_id: str, now: Optional[datetime] = None) -> dict:
        """查看成员账况。"""
        decided = self._resolve_now(now)
        ts = self._ts(decided)
        row = self._conn.execute(
            "SELECT redeemable, lifetime_received FROM members"
            " WHERE member_id = ?",
            (member_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown member: {member_id}")
        month_key = decided.strftime("%Y-%m")
        month_used = self._conn.execute(
            "SELECT COALESCE(SUM(points), 0) FROM gifts WHERE giver_id = ?"
            " AND strftime('%Y-%m', settled_at, 'unixepoch') = ?",
            (member_id, month_key),
        ).fetchone()[0]
        window_count = self._conn.execute(
            "SELECT COUNT(*) FROM gifts WHERE giver_id = ? AND settled_at > ?",
            (member_id, ts - WINDOW_SECONDS),
        ).fetchone()[0]
        return {
            "member_id": member_id,
            "redeemable": row[0],
            "lifetime_received": row[1],
            "month_used": month_used,
            "month_remaining": MONTHLY_QUOTA - month_used,
            "window_count": window_count,
            "window_remaining": WINDOW_MAX_COUNT - window_count,
        }
