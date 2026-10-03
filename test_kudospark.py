import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from kudospark import KudoSpark, MemberSeed, RewardSeed

UTC = timezone.utc


def dt(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


class KudoSparkTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "kudospark.db")

    def tearDown(self):
        self.tmp.cleanup()

    def open_db(self, members, rewards):
        return KudoSpark(self.db_path, members=members, rewards=rewards)


class WindowAcrossMonthTest(KudoSparkTestBase):
    """月末窗口限额跨月不清空；失败与成功请求重投均返回首次结果。"""

    def test_hour_window_survives_month_boundary(self):
        ks = self.open_db(
            members=[MemberSeed("g"), MemberSeed("r")], rewards=[]
        )
        t0 = dt(2026, 1, 31, 23, 50)
        for i in range(1, 21):
            res = ks.give("g", "r", 1, f"g-{i:02d}", t0)
            self.assertTrue(res.ok, f"gift {i} should succeed")
        acct = ks.account("g", t0)
        self.assertEqual(acct["month_used"], 20)
        self.assertEqual(acct["window_count"], 20)

        # 次月 00:00：月度额度恢复为 50，但滚动窗口仍满，赠分失败。
        t1 = dt(2026, 2, 1, 0, 0)
        acct = ks.account("g", t1)
        self.assertEqual(acct["month_remaining"], 50)
        fail = ks.give("g", "r", 1, "g-fail", t1)
        self.assertFalse(fail.ok)
        self.assertEqual(fail.error, "window_limit_exceeded")

        # 00:50：距 23:50 恰好 3600 秒，旧记录不再计入窗口，新编号成功。
        t2 = dt(2026, 2, 1, 0, 50)
        ok = ks.give("g", "r", 1, "g-new", t2)
        self.assertTrue(ok.ok)

        # 重投 00:00 的失败编号：仍返回首次失败结果与原结算时间。
        again = ks.give("g", "r", 1, "g-fail", t2)
        self.assertFalse(again.ok)
        self.assertEqual(again.error, "window_limit_exceeded")
        self.assertEqual(again.settled_at, t1)

        # 重投上月成功编号：返回首次成功结果，不重复到账。
        replay = ks.give("g", "r", 1, "g-01", t2)
        self.assertTrue(replay.ok)
        self.assertEqual(replay.settled_at, t0)
        self.assertEqual(ks.account("r", t2)["redeemable"], 21)
        ks.close()


class RedeemAndReopenTest(KudoSparkTestBase):
    """收分后兑换；重开数据库重复请求不改变账况。"""

    def test_redeem_and_replay_after_reopen(self):
        members = [
            MemberSeed("g"),
            MemberSeed("m", redeemable=149, lifetime_received=149),
        ]
        rewards = [RewardSeed("r100", 100)]
        t0 = dt(2026, 3, 10, 8, 0)

        ks = self.open_db(members, rewards)
        gift = ks.give("g", "m", 1, "gift-1", t0)
        self.assertTrue(gift.ok)
        redeem = ks.redeem("m", "r100", "redeem-1", t0)
        self.assertTrue(redeem.ok)
        acct = ks.account("m", t0)
        self.assertEqual(acct["redeemable"], 50)
        self.assertEqual(acct["lifetime_received"], 150)
        ks.close()

        # 重新打开数据库，重复两笔请求。
        ks = self.open_db(members, rewards)
        t1 = t0 + timedelta(days=30)
        gift2 = ks.give("g", "m", 1, "gift-1", t1)
        self.assertTrue(gift2.ok)
        self.assertEqual(gift2.settled_at, t0)
        redeem2 = ks.redeem("m", "r100", "redeem-1", t1)
        self.assertTrue(redeem2.ok)
        self.assertEqual(redeem2.settled_at, t0)
        acct = ks.account("m", t1)
        self.assertEqual(acct["redeemable"], 50)
        self.assertEqual(acct["lifetime_received"], 150)
        ks.close()


class GiveValidationTest(KudoSparkTestBase):
    def setUp(self):
        super().setUp()
        self.ks = self.open_db(
            members=[MemberSeed("a"), MemberSeed("b")],
            rewards=[RewardSeed("r10", 10)],
        )
        self.now = dt(2026, 5, 1, 12, 0)

    def tearDown(self):
        self.ks.close()
        super().tearDown()

    def test_self_gift_rejected(self):
        res = self.ks.give("a", "a", 1, "s-1", self.now)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "self_gift")

    def test_points_out_of_range(self):
        for i, pts in enumerate((0, 6, -1)):
            res = self.ks.give("a", "b", pts, f"p-{i}", self.now)
            self.assertFalse(res.ok)
            self.assertEqual(res.error, "invalid_points")

    def test_unknown_member(self):
        res = self.ks.give("ghost", "b", 1, "u-1", self.now)
        self.assertEqual(res.error, "unknown_giver")
        res = self.ks.give("a", "ghost", 1, "u-2", self.now)
        self.assertEqual(res.error, "unknown_receiver")

    def test_monthly_quota_not_carried(self):
        for i in range(10):
            res = self.ks.give("a", "b", 5, f"q-{i}", self.now)
            self.assertTrue(res.ok)
        over = self.ks.give("a", "b", 1, "q-over", self.now)
        self.assertEqual(over.error, "monthly_quota_exceeded")
        nxt = dt(2026, 6, 1, 0, 0)
        ok = self.ks.give("a", "b", 5, "q-next", nxt)
        self.assertTrue(ok.ok)


class IdempotencyTest(KudoSparkTestBase):
    def setUp(self):
        super().setUp()
        self.ks = self.open_db(
            members=[MemberSeed("a"), MemberSeed("b")],
            rewards=[RewardSeed("r10", 10)],
        )
        self.now = dt(2026, 5, 1, 12, 0)

    def tearDown(self):
        self.ks.close()
        super().tearDown()

    def test_same_id_different_content_rejected(self):
        self.assertTrue(self.ks.give("a", "b", 2, "x-1", self.now).ok)
        conflict = self.ks.give("a", "b", 3, "x-1", self.now)
        self.assertFalse(conflict.ok)
        self.assertEqual(conflict.error, "request_id_conflict")
        self.assertEqual(self.ks.account("b")["redeemable"], 2)

    def test_shared_namespace_across_ops(self):
        self.assertTrue(self.ks.give("a", "b", 5, "shared", self.now).ok)
        res = self.ks.redeem("a", "r10", "shared", self.now)
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "request_id_conflict")

    def test_failed_redeem_not_retried_after_balance_grows(self):
        poor = self.ks.redeem("b", "r10", "rd-1", self.now)
        self.assertEqual(poor.error, "insufficient_balance")
        self.assertTrue(self.ks.give("a", "b", 5, "g-1", self.now).ok)
        self.assertTrue(self.ks.give("a", "b", 5, "g-2", self.now).ok)
        later = self.now + timedelta(hours=2)
        replay = self.ks.redeem("b", "r10", "rd-1", later)
        self.assertFalse(replay.ok)
        self.assertEqual(replay.error, "insufficient_balance")
        self.assertEqual(replay.settled_at, self.now)
        self.assertEqual(self.ks.account("b")["redeemable"], 10)


if __name__ == "__main__":
    unittest.main()
