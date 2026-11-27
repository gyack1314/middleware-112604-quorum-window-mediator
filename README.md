# 联合演练窗口确认中间件（window_confirm）

多个站点共同确认是否开放一个**共享软件模拟窗口**。站点退出、换值班人员、消息
迟到时，旧确认不会被混进新成员集合；只有在“本次提案版本 + 本次公布的成员配置”
下真实满足条件、且不与同资源范围窗口互斥时，才签发可执行授权。每条派给模拟
执行端的指令都有 `auth_id`、依据摘要和只追加的哈希链事件日志，可追查、可在
重启后恢复真实执行边界。

> 只管理软件模拟窗口，不连接任何真实设备。Python 3.11，仅标准库。

---

## 1. 快速验证

```bash
# 原有：核对演练资料格式与读取器
python3 scenario_reader.py examples
python3 -m unittest discover -v

# 中间件：回放两个内置场景，打印全过程并校验关键结论
python3 -m window_confirm demo
```

测试共 27 个用例，覆盖：两个 examples 的精确轨迹、过渡双条件、票与 roster
绑定、版本失效、迟到票/前任成员、领取与开始边界、互斥与跨 scope、重启恢复、
哈希链防篡改、真实 HTTP API。

## 2. 一分钟体验

把演练 JSONL 直接回放（每行消息按 `at`、同秒按行序提交）：

```bash
python3 -m window_confirm replay examples/joint-membership-transition.jsonl --text

# 落盘后，模拟“重启后运营检查”
python3 -m window_confirm replay examples/withdraw-after-real-start.jsonl \
    --data-dir /tmp/wc-drill
python3 -m window_confirm inspect --data-dir /tmp/wc-drill --id y
python3 -m window_confirm verify  --data-dir /tmp/wc-drill
```

启动常驻 HTTP 服务，持续接收新成员、提案、投票、授权领取与终态回报：

```bash
python3 -m window_confirm serve --host 0.0.0.0 --port 8080 --data-dir .window_confirm
# 或 python3 server.py --port 8080
curl -s localhost:8080/            # 接口索引
```

## 3. 输入消息契约（与 `examples/message_types.json` 一致）

每个 JSONL 文件是一段独立输入记录。每行包括整数秒点 `at`、消息名称 `kind` 与
对象 `record`。`at` 是中间件收到消息的时刻，同一秒内按行序提交。

- **roster**　scope 的 revision，members、minimum、required 与 prev。`prev`
  非空时进入过渡期：**新、旧两套条件同时检查**。可选用
  `site_of: {成员: 独立站点}` 把同一站点的多名值班员合并计为一个独立站点；
  缺省每个合成成员标识即一个独立站点。
- **proposal**　scope 的 id、rev、roster 和 text（或任意 `content`）。
  `(id, rev)` 内容不可变；修改提案必须使用更大的 rev，旧 rev 的赞成不继承。
- **vote**　member 对 id/rev 给 `yes`、`no` 或 `withdraw`，并**引用 roster**。
- **grant**　`{id, rev}`，要求当场判定是否能下发；条件满足且互斥槽空闲才签发。
- **started**　模拟端接受指定 id/rev 的已授动作（真实执行开始边界）。
- **settled**　模拟端回报 state，`done` 或 `stopped`，附相同 id/rev。
- **restart**　模拟服务/中间件重启；服务从事件日志重建。
- **inspect**　查看 scope 或 id。
- **claim**（执行端领取，HTTP API；JSONL 契约之外的显式领取动作）

示例：

- `examples/joint-membership-transition.jsonl`
- `examples/withdraw-after-real-start.jsonl`

## 4. 核心判定规则（为什么这样才“真的够数”）

### 4.1 一票只属于它引用的那套成员配置

计票槽位是 `(id, rev, vote.roster, member)`。一张票只有在
`member ∈ roster[vote.roster].members` 时才有资格计入该 roster 的条件。
因此：

- 旧 roster r1 的赞成**不会**被混入新 roster r2 的名单（修复原始事故）；
- 重叠成员（如同时在 r1/r2 的 `b`）要分别用 `roster:1`、`roster:2`
  各确认一次——这正是示例第 4、6 行两次 `b yes` 的含义；
- 被替换掉的成员（如 `a`）引用新 roster 的票判为 `not_member_of_roster`，
  只作为历史留存；它的确认只能用于它仍在任的旧组那一边；
- 无资格成员的反对/撤回不影响有效授权。

### 4.2 过渡期同时偿还“新组、旧组、指定责任方”的欠账

提案绑定 roster r2，而 r2 声明 `prev: 1` 时，授权必须同时满足：

- r2（新组）：独立站点赞成数 ≥ `minimum`，且 `required` 全部赞成；
- r1（旧组）：同样满足其 `minimum` 与 `required`（例如旧组必须有 `a`）。

`grant` 拒绝与 `inspect` 会给出结构化 `owed`/`blockers`，并配中文
`fact_zh`，明确“尚欠旧组 N 个独立站点 / 新组缺指定责任方 d”。

过渡的开始由带 `prev` 的 roster 公布；运营公布一个不带 `prev` 的新 roster
修订即结束双条件过渡期。

### 4.3 提案版本不可混用

投票带 rev，新 rev 从零计票；“一条已修改的提案不能继续算旧赞成”。登记新 rev
时，旧 rev **尚未开始**的授权会被标记 `superseded` 并释放互斥槽；已经
`active` 的旧动作继续走到终态，期间新 rev 的授权被互斥挡住。

### 4.4 scope 即互斥范围

每个 scope（资源范围）最多有一个持有互斥槽的授权，状态为
`valid / claimed / active`。第二个并行窗口即使法定人数已足，`grant` 也返回
`blocked_mutex` 并指出占用者。`settled(done|stopped)` 后槽位释放。
**另一个 scope 的窗口不受影响，继续处理。**

### 4.5 授权生命周期与“能不能撤回”

```
 grant 判定通过
      │ 签发 AUTH-xxxxx（status=valid，冻结 version_basis）
      ▼
 claim 领取指令（可重复领取，返回同一指令；记录 executor/次数）
      │ 首次领取 → claimed
      ▼
 started 模拟端已接受动作 ──→ active（真实执行开始边界）
      │
      ▼
 settled done | stopped ───→ 终态（真实执行结束边界，释放互斥）
```

- **未开始之前**（`valid`/`claimed`）：新的 `no/withdraw` 使冻结的那套条件
  不再满足时，授权被置为 `revoked`；之后领取得到 `revoked`，再 `started`
  会被拒绝。
- **已经 started（active）之后**：反对/撤回只记录为异议与
  `stop_advisory`，**不能撤销执行、不能给互斥窗口另发新指令**；必须等待真实的
  `done/stopped` 回报。
- **已撤授权永不复活**：状态对象停在 `revoked`。前任站点迟到的同意只进历史；
  即使条件重新凑齐，再次 `grant` 也只会产生一个**新的 `AUTH-id`**
  （decision=`reissued`），旧授权仍是撤销态。
- `done/stopped` 是固定边界：冲突的第二个终态回报只记审计、不覆盖；
  同一 `(id, rev)` 不能重新授权，需要再次演练请提新 rev。
- 重复 `grant` 在存在有效授权时幂等返回同一 `auth_id`，不会重复签发/下发。

## 5. 重启与可追查性

- 所有输入与关键结果以 JSONL 追加到 `<data-dir>/eventlog.jsonl`，
  `flush + fsync`，每条事件含前一条哈希（SHA-256 链）。
- 内存状态完全由事件日志折叠得到。真实进程重启 =
  `ConfirmationService(data_dir)` 重新回放；场景中的 `restart` 消息 =
  `service.simulate_restart()`，先落盘再从同一日志重建。
- 重启后 `inspect` 仍给出：
  - `scope_execution_frontier.mutex_holder`：当前谁占着互斥；
  - `last_started` / `last_settled`：真实开始/终态边界（带 at/seq/auth_id）；
  - `blockers`：现在仍不能动的具体原因（欠票、互斥占用、已撤销、已终态…）。
- 每次授权的 `basis` 冻结：提案 rev 与内容哈希、引用的 roster 修订及完整
  members/minimum/required、计入的选票（member/site/at/seq）与
  `basis_digest`。指令信封含 `auth_id`、一次性领取 `token`、内容哈希和回调路径。
- `GET /v1/events` 或 `python3 -m window_confirm verify --data-dir DIR`
  可校验哈希链；篡改事件会导致哈希校验失败。

## 6. HTTP / JSON 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/messages` | 统一入口，body 为 `{at?, kind, record}`，可直接喂 JSONL 每一行 |
| POST | `/v1/rosters` | 公布/追加成员配置修订 |
| POST | `/v1/proposals` | 提交窗口提案版本 |
| POST | `/v1/votes` | yes / no / withdraw |
| POST | `/v1/grants` | 授权判定与签发 |
| POST | `/v1/authorizations/claim` | 模拟执行端领取指令（`auth_id+token` 或 `id+rev`） |
| POST | `/v1/executions/started` | 开始回报 |
| POST | `/v1/executions/settled` | `done/stopped` 终态回报 |
| POST | `/v1/restart` | 从磁盘事件日志重建（演练用；生产直接重启进程即可） |
| GET | `/v1/inspect?id=&rev=&scope=` | 窗口或范围检查 |
| GET | `/v1/windows/{id}` | 单窗口完整依据/授权/边界/阻塞原因 |
| GET | `/v1/scopes/{scope}` | 资源范围与互斥前沿 |
| GET | `/v1/events` | 全量事件与哈希链校验 |
| GET | `/healthz`、`/` | 健康检查、接口索引 |

完整 curl 流程：

```bash
B=localhost:8080
curl -s -X POST $B/v1/rosters -H 'Content-Type: application/json' -d '{
  "scope":"line","revision":1,"members":["a","b"],"minimum":2,"required":[]}'
curl -s -X POST $B/v1/proposals -H 'Content-Type: application/json' -d '{
  "scope":"line","id":"w","rev":1,"roster":1,"text":"open"}'
curl -s -X POST $B/v1/votes -H 'Content-Type: application/json' -d '{
  "id":"w","rev":1,"roster":1,"member":"a","value":"yes"}'
curl -s -X POST $B/v1/votes -H 'Content-Type: application/json' -d '{
  "id":"w","rev":1,"roster":1,"member":"b","value":"yes"}'
curl -s -X POST $B/v1/grants -H 'Content-Type: application/json' -d '{"id":"w","rev":1}'
# -> granted，返回 command.auth_id / command.token
curl -s -X POST $B/v1/authorizations/claim -H 'Content-Type: application/json' \
  -d '{"id":"w","rev":1,"executor_id":"sim-1"}'
curl -s -X POST $B/v1/executions/started -H 'Content-Type: application/json' \
  -d '{"id":"w","rev":1}'
curl -s localhost:8080/v1/windows/w | python3 -m json.tool
curl -s -X POST $B/v1/executions/settled -H 'Content-Type: application/json' \
  -d '{"id":"w","rev":1,"state":"done"}'
```

## 7. Python 接口

```python
from pathlib import Path
from window_confirm import ConfirmationService
from window_confirm.simulator import SimulatedExecutor

svc = ConfirmationService(Path("/tmp/wc-state"))   # 目录持久化；不传则临时目录
svc.register_roster({"scope": "line", "revision": 1,
                     "members": ["a", "b"], "minimum": 2, "required": ["a"]}, at=0)
svc.propose({"scope": "line", "id": "go", "rev": 1,
             "roster": 1, "text": "open"}, at=1)
svc.vote({"id": "go", "rev": 1, "roster": 1, "member": "a", "value": "yes"}, at=2)
svc.vote({"id": "go", "rev": 1, "roster": 1, "member": "b", "value": "yes"}, at=2)
grant = svc.request_grant({"id": "go", "rev": 1}, at=3)
assert grant["granted"]

exe = SimulatedExecutor(svc)
exe.fetch("go", 1, at=4)        # claim + started
exe.finish("go", 1, "done", at=5)

report = svc.inspect({"id": "go"})
print(report["version_basis"])           # 依据的提案/roster 版本与完整配置
print(report["current_authorization"])   # 获权时点、状态、是否仍可撤回
print(report["blockers"])                # 现在为什么不能动（若有）
svc.close()

# 真实进程重启：ConfirmationService(Path("/tmp/wc-state")) 即恢复全部事实
```

回放文件：

```python
from window_confirm.runner import run_file
service, outputs = run_file("examples/joint-membership-transition.jsonl",
                            data_dir="/tmp/wc-joint")
```

## 8. 从一个窗口能看到什么

`inspect id`（或 `GET /v1/windows/{id}`）回答题目的三个追问：

1. **依据哪个版本/成员配置**：`version_basis.proposal_revision`、
   `content_sha256`、`bound_roster_revision`、过渡期新旧 roster 的完整
   members/minimum/required；授权里另有冻结的 `basis` 与 `basis_digest`。
2. **什么时点已获权**：`current_authorization.issued_at`（演练 `at` 秒点）、
   `auth_id`、领取/开始/终态各时点。
3. **是否还能撤回**：`withdrawable/revocable_before_start` 与 `revoke_hint`；
   一旦 `started_at` 非空即为 `false`，必须等终态回报。

同时给出 `quorum.owed`（欠新组/旧组/指定成员的事实）、`vote_history`
（含无资格票及原因）、`authorizations`（历次签发与撤销，旧 id 不复活）、
`scope_execution_frontier`（真实执行边界与互斥占用）、`blockers`。

## 9. 项目结构

```
window_confirm/
  __init__.py     公共导出
  store.py        哈希链 JSONL 事件存储（fsync）与链校验
  engine.py       纯领域引擎：计票/过渡条件/版本/互斥/授权状态机/inspect
  service.py      串行化 + 持久化编排 + 重启重建 + 统一消息入口
  runner.py       复用 scenario_reader 回放 JSONL
  simulator.py    纯软件模拟执行端（claim→started→settled）
  httpapi.py      标准库 ThreadingHTTPServer JSON API
  render.py       中文 inspect 文本视图
  cli.py          replay / serve / inspect / verify / demo
scenario_reader.py        既有演练资料读取器（保持不变）
server.py                 HTTP 服务便捷入口
test_*.py                 27 个 unittest 用例
examples/                 输入契约与两个演练场景
```

## 10. 输入消息（原始读取器说明）

资料读取器会按文件统计各类参与方消息的数量。演练记录只描述各参与方交给系统的
输入；运行：

```bash
python3 scenario_reader.py examples
```
