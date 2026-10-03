# task26100523

KudoSpark 荣誉积分结算库：Python + SQLite，由上层直接调用，成员账况与请求结果持久化在本地数据库。

## 能力

- `give(giver_id, receiver_id, points, request_id, now=None)`：赠分（1–5 分，不能赠自己）。
- `redeem(member_id, reward_id, request_id, now=None)`：按奖励标价兑换，余额不足整笔拒绝。
- `account(member_id, now=None)`：查看可兑余额、终身收分、当月已用/剩余额度、滚动窗口笔数。

## 规则

- 每人每 UTC 自然月可赠 50 分，余额不结转；新月首笔自动使用新月额度。
- 每名赠送者任意滚动 3600 秒内最多成功赠分 20 笔；恰好 3600 秒前的记录不计入；跨月不清空窗口。
- 成功赠分同时增加接收者可兑余额与终身收分；兑换只扣可兑余额。
- 请求编号按成员共享赠分/兑换命名空间：相同编号相同内容返回首次结果（含首次拒绝）并沿用原结算时间；编号相同内容不同则拒绝且不改账。

## 用法

```python
from datetime import datetime, timezone
from kudospark import KudoSpark, MemberSeed, RewardSeed

ks = KudoSpark(
    "kudospark.db",
    members=[MemberSeed("alice"), MemberSeed("bob", redeemable=149, lifetime_received=149)],
    rewards=[RewardSeed("coffee", 100)],
)
res = ks.give("alice", "bob", 3, "req-001", datetime.now(timezone.utc))
print(res.to_dict())
print(ks.account("bob"))
ks.close()
```

`now` 缺省取当前 UTC 时间；业务时间以首次作出结算决定时的 UTC 时间为准。

## 测试

```bash
python3 -m unittest test_kudospark -v
```
