"""KudoSpark 荣誉积分结算库。

由上层直接调用，成员账况与请求结果持久化在本地 SQLite 数据库中。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

MONTHLY_GRANT_QUOTA = 50
GRANT_WINDOW_SECONDS = 3600
GRANT_WINDOW_MAX_COUNT = 20
MIN_GRANT_POINTS = 1
MAX_GRANT_POINTS = 5


@dataclass(frozen=True)
class Member:
    member_id: str
    name: str = ""


@dataclass(frozen=True)
class Reward:
    reward_id: str
    price: int
    name: str = ""


@dataclass(frozen=True)
class OpResult:
    ok: bool
    reason: str = ""
    settled_at: str = ""
    replayed: bool = False


@dataclass(frozen=True)
class AccountView:
    member_id: str
    redeemable_balance: int
    lifetime_received: int
    monthly_grant_remaining: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    member_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    redeemable_balance INTEGER NOT NULL DEFAULT 0,
    lifetime_received INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rewards (
    reward_id TEXT PRIMARY KEY,
    price INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS requests (
    member_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    content TEXT NOT NULL,
    ok INTEGER NOT NULL,
    reason TEXT NOT NULL,
    settled_at INTEGER NOT NULL,
    PRIMARY KEY (member_id, request_id)
);
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    giver_id TEXT NOT NULL,
    receiver_id TEXT NOT NULL,
    points INTEGER NOT NULL,
    settled_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grants_giver_time ON grants (giver_id, settled_at);
"""


def _to_epoch(now: Optional[datetime]) -> int:
    if now is None:
        return int(datetime.now(timezone.utc).timestamp())
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return int(now.timestamp())


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _month_start_epoch(epoch: int) -> int:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return int(datetime(dt.year, dt.month, 1, tzinfo=timezone.utc).timestamp())


class KudoSpark:
    def __init__(
        self,
        db_path: str,
        members: list[Member],
        rewards: list[Reward],
    ) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO members (member_id, name) VALUES (?, ?)",
                [(m.member_id, m.name) for m in members],
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO rewards (reward_id, price, name) VALUES (?, ?, ?)",
                [(r.reward_id, r.price, r.name) for r in rewards],
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "KudoSpark":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _member_exists(self, member_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM members WHERE member_id = ?", (member_id,)
        ).fetchone()
        return row is not None

    def _settle(
        self,
        member_id: str,
        request_id: str,
        content: dict,
        now: Optional[datetime],
        decide,
    ) -> OpResult:
        epoch = _to_epoch(now)
        content_json = json.dumps(content, sort_keys=True)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT content, ok, reason, settled_at FROM requests "
                "WHERE member_id = ? AND request_id = ?",
                (member_id, request_id),
            ).fetchone()
            if row is not None:
                old_content, old_ok, old_reason, old_settled_at = row
                self._conn.rollback()
                if old_content == content_json:
                    return OpResult(
                        ok=bool(old_ok),
                        reason=old_reason,
                        settled_at=_iso(old_settled_at),
                        replayed=True,
                    )
                return OpResult(
                    ok=False,
                    reason="request_id_conflict",
                    settled_at=_iso(old_settled_at),
                    replayed=True,
                )
            ok, reason, apply = decide(epoch)
            if ok:
                apply()
            self._conn.execute(
                "INSERT INTO requests (member_id, request_id, content, ok, reason, settled_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (member_id, request_id, content_json, int(ok), reason, epoch),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return OpResult(ok=ok, reason=reason, settled_at=_iso(epoch))

    def grant(
        self,
        request_id: str,
        giver_id: str,
        receiver_id: str,
        points: int,
        now: Optional[datetime] = None,
    ) -> OpResult:
        content = {
            "op": "grant",
            "giver": giver_id,
            "receiver": receiver_id,
            "points": points,
        }

        def decide(epoch: int):
            if not isinstance(points, int) or not (MIN_GRANT_POINTS <= points <= MAX_GRANT_POINTS):
                return False, "invalid_points", None
            if giver_id == receiver_id:
                return False, "self_grant", None
            if not self._member_exists(giver_id) or not self._member_exists(receiver_id):
                return False, "unknown_member", None
            used = self._conn.execute(
                "SELECT COALESCE(SUM(points), 0) FROM grants WHERE giver_id = ? AND settled_at >= ?",
                (giver_id, _month_start_epoch(epoch)),
            ).fetchone()[0]
            if used + points > MONTHLY_GRANT_QUOTA:
                return False, "monthly_quota_exceeded", None
            window_count = self._conn.execute(
                "SELECT COUNT(*) FROM grants WHERE giver_id = ? AND settled_at > ?",
                (giver_id, epoch - GRANT_WINDOW_SECONDS),
            ).fetchone()[0]
            if window_count >= GRANT_WINDOW_MAX_COUNT:
                return False, "rate_limit_exceeded", None

            def apply() -> None:
                self._conn.execute(
                    "INSERT INTO grants (giver_id, receiver_id, points, settled_at) VALUES (?, ?, ?, ?)",
                    (giver_id, receiver_id, points, epoch),
                )
                self._conn.execute(
                    "UPDATE members SET redeemable_balance = redeemable_balance + ?, "
                    "lifetime_received = lifetime_received + ? WHERE member_id = ?",
                    (points, points, receiver_id),
                )

            return True, "", apply

        return self._settle(giver_id, request_id, content, now, decide)

    def redeem(
        self,
        request_id: str,
        member_id: str,
        reward_id: str,
        now: Optional[datetime] = None,
    ) -> OpResult:
        content = {"op": "redeem", "member": member_id, "reward": reward_id}

        def decide(epoch: int):
            if not self._member_exists(member_id):
                return False, "unknown_member", None
            row = self._conn.execute(
                "SELECT price FROM rewards WHERE reward_id = ?", (reward_id,)
            ).fetchone()
            if row is None:
                return False, "unknown_reward", None
            price = row[0]
            balance = self._conn.execute(
                "SELECT redeemable_balance FROM members WHERE member_id = ?",
                (member_id,),
            ).fetchone()[0]
            if balance < price:
                return False, "insufficient_balance", None

            def apply() -> None:
                self._conn.execute(
                    "UPDATE members SET redeemable_balance = redeemable_balance - ? WHERE member_id = ?",
                    (price, member_id),
                )

            return True, "", apply

        return self._settle(member_id, request_id, content, now, decide)

    def account(self, member_id: str, now: Optional[datetime] = None) -> AccountView:
        epoch = _to_epoch(now)
        row = self._conn.execute(
            "SELECT redeemable_balance, lifetime_received FROM members WHERE member_id = ?",
            (member_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown member: {member_id}")
        balance, lifetime = row
        used = self._conn.execute(
            "SELECT COALESCE(SUM(points), 0) FROM grants WHERE giver_id = ? AND settled_at >= ?",
            (member_id, _month_start_epoch(epoch)),
        ).fetchone()[0]
        return AccountView(
            member_id=member_id,
            redeemable_balance=balance,
            lifetime_received=lifetime,
            monthly_grant_remaining=MONTHLY_GRANT_QUOTA - used,
        )
