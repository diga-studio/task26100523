"""KudoSpark 自动化测试。"""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from kudospark import KudoSpark, Member, Reward

UTC = timezone.utc
JAN_31_2350 = datetime(2025, 1, 31, 23, 50, 0, tzinfo=UTC)
FEB_01_0000 = datetime(2025, 2, 1, 0, 0, 0, tzinfo=UTC)
FEB_01_0050 = datetime(2025, 2, 1, 0, 50, 0, tzinfo=UTC)

MEMBERS = [Member("alice"), Member("bob"), Member("carol"), Member("dave")]
REWARDS = [Reward("r100", 100, "book"), Reward("r10", 10, "sticker")]


class Base(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.ks = KudoSpark(self.db_path, MEMBERS, REWARDS)

    def tearDown(self):
        self.ks.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def reopen(self):
        self.ks.close()
        self.ks = KudoSpark(self.db_path, MEMBERS, REWARDS)


class MonthBoundaryRateLimitTest(Base):
    def test_hour_limit_survives_month_reset_and_replays(self):
        # 月末 23:50 成功赠送 20 笔各 1 分
        for i in range(20):
            res = self.ks.grant(f"jan-{i:02d}", "alice", "bob", 1, now=JAN_31_2350)
            self.assertTrue(res.ok, f"grant jan-{i:02d} failed: {res.reason}")
        self.assertEqual(self.ks.account("bob", now=JAN_31_2350).redeemable_balance, 20)

        # 次月 00:00 可赠额度恢复为 50，但小时限额仍为 20 笔
        self.assertEqual(self.ks.account("alice", now=FEB_01_0000).monthly_grant_remaining, 50)
        res = self.ks.grant("feb-fail", "alice", "bob", 1, now=FEB_01_0000)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "rate_limit_exceeded")

        # 00:50 旧记录恰好满 3600 秒不再计入，使用新编号可以成功
        res = self.ks.grant("feb-ok", "alice", "bob", 1, now=FEB_01_0050)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(self.ks.account("bob", now=FEB_01_0050).redeemable_balance, 21)
        self.assertEqual(self.ks.account("alice", now=FEB_01_0050).monthly_grant_remaining, 49)

        # 重投 00:00 的失败编号仍然失败（返回首次拒绝结果）
        res = self.ks.grant("feb-fail", "alice", "bob", 1, now=FEB_01_0050)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "rate_limit_exceeded")
        self.assertTrue(res.replayed)

        # 重投上月成功编号不能再次到账
        res = self.ks.grant("jan-00", "alice", "bob", 1, now=FEB_01_0050)
        self.assertTrue(res.ok)
        self.assertTrue(res.replayed)
        self.assertEqual(res.settled_at, JAN_31_2350.isoformat())
        self.assertEqual(self.ks.account("bob", now=FEB_01_0050).redeemable_balance, 21)
        self.assertEqual(self.ks.account("alice", now=FEB_01_0050).monthly_grant_remaining, 49)


class RedeemAndReopenTest(Base):
    def test_balance_lifetime_and_idempotent_replay_after_reopen(self):
        # 构造终身收分与可兑余额均为 149 的成员
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE members SET redeemable_balance = 149, lifetime_received = 149 "
            "WHERE member_id = 'carol'"
        )
        conn.commit()
        conn.close()

        # 收到 1 分：可兑余额与终身收分同时 +1
        res = self.ks.grant("g1", "dave", "carol", 1, now=FEB_01_0000)
        self.assertTrue(res.ok, res.reason)
        acct = self.ks.account("carol", now=FEB_01_0000)
        self.assertEqual(acct.redeemable_balance, 150)
        self.assertEqual(acct.lifetime_received, 150)

        # 兑换价格 100 的奖励：只扣可兑余额，终身收分不变
        res = self.ks.redeem("r1", "carol", "r100", now=FEB_01_0000 + timedelta(seconds=1))
        self.assertTrue(res.ok, res.reason)
        acct = self.ks.account("carol", now=FEB_01_0000)
        self.assertEqual(acct.redeemable_balance, 50)
        self.assertEqual(acct.lifetime_received, 150)

        # 重新打开数据库后重复这两笔请求，账况不变
        self.reopen()
        res = self.ks.grant("g1", "dave", "carol", 1, now=FEB_01_0050)
        self.assertTrue(res.ok)
        self.assertTrue(res.replayed)
        self.assertEqual(res.settled_at, FEB_01_0000.isoformat())
        res = self.ks.redeem("r1", "carol", "r100", now=FEB_01_0050)
        self.assertTrue(res.ok)
        self.assertTrue(res.replayed)
        acct = self.ks.account("carol", now=FEB_01_0050)
        self.assertEqual(acct.redeemable_balance, 50)
        self.assertEqual(acct.lifetime_received, 150)


class GrantRuleTest(Base):
    def test_invalid_points_and_self_grant_rejected_without_cost(self):
        now = FEB_01_0000
        for points in (0, 6, -1):
            res = self.ks.grant(f"bad-{points}", "alice", "bob", points, now=now)
            self.assertFalse(res.ok)
            self.assertEqual(res.reason, "invalid_points")
        res = self.ks.grant("self", "alice", "alice", 1, now=now)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "self_grant")
        # 失败请求不消耗额度与窗口
        self.assertEqual(self.ks.account("alice", now=now).monthly_grant_remaining, 50)
        res = self.ks.grant("ok-1", "alice", "bob", 5, now=now)
        self.assertTrue(res.ok, res.reason)

    def test_monthly_quota_50_and_reset_next_month(self):
        # 分散在多个小时，避开 20 笔/小时窗口
        t = datetime(2025, 1, 30, 0, 0, 0, tzinfo=UTC)
        for i in range(10):
            res = self.ks.grant(
                f"q-{i}", "alice", "bob", 5, now=t + timedelta(hours=2 * i)
            )
            self.assertTrue(res.ok, res.reason)
        last = t + timedelta(hours=18)
        self.assertEqual(self.ks.account("alice", now=last).monthly_grant_remaining, 0)
        res = self.ks.grant("q-over", "alice", "bob", 1, now=last + timedelta(hours=2))
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "monthly_quota_exceeded")
        # 次月额度恢复（余额不结转，固定 50）
        next_month = datetime(2025, 2, 5, 12, 0, 0, tzinfo=UTC)
        self.assertEqual(self.ks.account("alice", now=next_month).monthly_grant_remaining, 50)
        res = self.ks.grant("q-feb", "alice", "bob", 5, now=next_month)
        self.assertTrue(res.ok, res.reason)

    def test_unknown_member_and_reward(self):
        now = FEB_01_0000
        res = self.ks.grant("u1", "alice", "nobody", 1, now=now)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "unknown_member")
        res = self.ks.redeem("u2", "alice", "no-such-reward", now=now)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "unknown_reward")


class RedeemRuleTest(Base):
    def test_insufficient_balance_rejects_whole_redeem(self):
        res = self.ks.grant("g1", "alice", "bob", 5, now=FEB_01_0000)
        self.assertTrue(res.ok, res.reason)
        res = self.ks.redeem("r1", "bob", "r100", now=FEB_01_0000)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "insufficient_balance")
        # 整笔拒绝，余额不变
        self.assertEqual(self.ks.account("bob", now=FEB_01_0000).redeemable_balance, 5)
        # 兑换不影响任何人的可赠额度与终身收分
        res = self.ks.redeem("r2", "bob", "r10", now=FEB_01_0000)
        self.assertFalse(res.ok)
        res = self.ks.grant("g2", "alice", "bob", 5, now=FEB_01_0000)
        self.assertTrue(res.ok, res.reason)
        res = self.ks.redeem("r3", "bob", "r10", now=FEB_01_0000)
        self.assertTrue(res.ok, res.reason)
        acct = self.ks.account("bob", now=FEB_01_0000)
        self.assertEqual(acct.redeemable_balance, 0)
        self.assertEqual(acct.lifetime_received, 10)
        self.assertEqual(self.ks.account("alice", now=FEB_01_0000).monthly_grant_remaining, 40)


class IdempotencyTest(Base):
    def test_same_id_different_content_rejected(self):
        now = FEB_01_0000
        res = self.ks.grant("x1", "alice", "bob", 1, now=now)
        self.assertTrue(res.ok, res.reason)
        # 同编号不同内容：拒绝且不改变账况
        res = self.ks.grant("x1", "alice", "bob", 2, now=now)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "request_id_conflict")
        res = self.ks.grant("x1", "alice", "carol", 1, now=now)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "request_id_conflict")
        self.assertEqual(self.ks.account("bob", now=now).redeemable_balance, 1)
        self.assertEqual(self.ks.account("carol", now=now).redeemable_balance, 0)

    def test_grant_and_redeem_share_id_space_per_member(self):
        now = FEB_01_0000
        self.ks.grant("g1", "alice", "bob", 5, now=now)
        self.ks.grant("g2", "alice", "bob", 5, now=now)
        res = self.ks.redeem("shared", "bob", "r10", now=now)
        self.assertTrue(res.ok, res.reason)
        # 同一成员赠分与兑换共用编号空间
        res = self.ks.grant("shared", "bob", "alice", 1, now=now)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "request_id_conflict")
        self.assertEqual(self.ks.account("alice", now=now).redeemable_balance, 0)

    def test_failure_replay_keeps_original_result(self):
        now = FEB_01_0000
        res = self.ks.redeem("f1", "bob", "r100", now=now)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "insufficient_balance")
        # 余额后来增加，重投仍返回首次拒绝结果
        for giver in ("alice", "carol", "dave"):
            for i in range(10):
                res = self.ks.grant(
                    f"{giver}-{i}", giver, "bob", 5,
                    now=now + timedelta(hours=2 * i),
                )
                self.assertTrue(res.ok, res.reason)
        acct = self.ks.account("bob", now=now + timedelta(hours=20))
        self.assertGreaterEqual(acct.redeemable_balance, 100)
        res = self.ks.redeem("f1", "bob", "r100", now=now + timedelta(hours=20))
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "insufficient_balance")
        self.assertTrue(res.replayed)
        self.assertEqual(res.settled_at, now.isoformat())


if __name__ == "__main__":
    unittest.main()
