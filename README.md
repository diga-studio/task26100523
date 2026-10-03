# KudoSpark 荣誉积分结算库

Python + SQLite 实现，由上层直接调用，成员账况与请求结果持久化在本地数据库。

## 能力

- `grant(request_id, giver_id, receiver_id, points, now=None)`：赠分（1–5 分，不能赠自己）。
  每人每 UTC 自然月 50 分额度（不结转），任意滚动 3600 秒内最多 20 笔成功赠分。
  成功时接收者可兑余额与终身收分同时增加。
- `redeem(request_id, member_id, reward_id, now=None)`：按奖励标价扣可兑余额，不足整笔拒绝。
- `account(member_id, now=None)`：查看可兑余额、终身收分、当月剩余可赠额度。

## 幂等

同一成员的赠分与兑换共用请求编号空间。相同编号且业务内容相同的重复请求返回首次
结果（含首次拒绝）并沿用原结算时间；编号相同内容不同则拒绝且不改账况。

## 用法

```python
from kudospark import KudoSpark, Member, Reward

ks = KudoSpark("kudo.db",
               members=[Member("alice"), Member("bob")],
               rewards=[Reward("book", 100)])
res = ks.grant("req-1", "alice", "bob", 3)
print(res.ok, res.reason)
print(ks.account("bob"))
ks.close()
```

`now` 参数可注入业务时间（UTC），缺省取当前 UTC 时间。

## 测试

```bash
python3 -m unittest test_kudospark -v
```
