# Broker 安全边界与 IBKR 状态

核对日期：**2026-11-21**。本模块仅完成离线模拟与查询型适配器的本地实现，**没有 IBKR 账号，没有 TWS/IB Gateway 联调，没有真实网络连接，没有订单发送或账号权限修改**。Mock 通过不代表 IBKR 可用、paper 环境已验证或策略有效。

## 当前可用与禁止项

| 项目 | 当前实现 |
| --- | --- |
| `SimulatedBroker` | 本地 SQLite 订单/成交日志，显式模拟成交，持久化去重、超时与恢复检查 |
| `IBKRAdapter` | 可选官方 `ibapi.client.EClient` / `ibapi.wrapper.EWrapper` 组合，延迟导入；仅允许账户、持仓、开放订单、成交和时间查询 |
| 真实连接 | 仅显式调用 `connect()`；默认禁用；Python < 3.11 在导入 API/创建 transport/打开 socket 前拒绝 |
| 当前本地环境 | `.venv/bin/python` 为 **Python 3.10.12**，可跑离线测试，不能连接当前官方 API |
| 环境确认 | `environment='UNKNOWN'`、`environment_verified=False`；配置 PAPER、账户匹配、DU 前缀和任何端口都不是 paper 证明 |
| 下单 / 撤单 / 全局取消 | IBKR `submit` 和 `cancel` 始终抛 `BrokerSafetyError`；发送器仅允许查询白名单，不发送 `placeOrder`、`cancelOrder`、`reqGlobalCancel` |
| Live | 永远禁止；没有 live 开关、环境认证覆盖开关或绕过参数 |
| 联调标记 | `integration_verified=False`，即使 mock 的全部恢复回调齐全也不变 |

只有协调器执行网关可调用券商；策略不应导入券商。独立风险检查属于网关前置职责，模拟器不会代替风险引擎。停新单不等于取消已有订单，更不会触发清仓。

公开导出：

```python
from quantlab.broker import SimulatedBroker, IBKRConfig, IBKRAdapter, BrokerSafetyError
```

导入和构造适配器不会连接；构造会创建/打开调用者指定的 SQLite 日志，其父目录须已经存在。模拟器与 IBKR 应使用独立日志文件；不得通过删除日志“修复”未知订单或重置 ID。

## 本地示例配置

根目录 `config.ibkr.example.json` 是用户要求的**非秘密、本地占位模板**，不是可直接连接的配置：

- `enabled=false`；不会自动加载或自动连接。
- `port=null`：刻意不猜测默认 paper/live 端口。
- `account=""`、`client_id=null`：不包含账号，必须由未来获授权的本地操作者明确填写；client ID 必须是正整数，禁止 client 0 接管订单。
- `host` 仅支持 `127.0.0.1` / `::1`。
- `environment` 只接受 `PAPER`，但只是请求意图，**不是服务器环境证据**。
- `official_api_version=null`：未来需核对官方本地下载的当前 Stable/Latest API 和匹配 TWS/Gateway 后填写安装分发版本，不应复制测试中的假版本。
- `timeout_seconds=10`、`heartbeat_seconds=15`、`max_requests_per_second=10` 为本地保守策略，不是券商保证或费率。

直接 `IBKRConfig(**json.load(...))` 读取未填写模板会因占位字段拒绝，属于预期 fail-closed 行为。即使填写并显式启用，在本地 Python 3.10 仍拒绝连接；即使未来 Python/依赖满足，也**只能进行显式查询，不能解锁下单或撤单**。

版本检查只比较 `importlib.metadata.version('ibapi')` 与明确配置版本并拒绝旧主版本，**不能证明软件来源、当前发行状态或 TWS 版本相符**。本任务没有下载/安装官方 SDK，也没有安装 PyPI 上可能过期的 `ibapi`。未来安装应遵循官方本地源码安装说明，由单独授权的操作完成；不得以改动权限、关闭只读或绕过版本检查来让本模块工作。

官方通用交易教程写有关闭 `Read-Only API` 的步骤；**本实现不照搬该步骤，不要求或执行关闭只读、不修改账号/API 权限**。

## 模拟器：订单证据、对账与 baseline

- `submit(intent)` 仅接受 SIMULATED。`intent_id`、`client_order_id` 双唯一；相同载荷重复提交返回原订单（包括 UNKNOWN），不同载荷复用 ID 拒绝。写入日志不隐式成交。
- `inject_submit_fault('timeout'|'disconnect')` 将新订单持久化为 UNKNOWN，阻断后续新单；不生成另一个 ID 重发。需实际模拟回调证据解决未知状态，再显式 `reconcile()`。`reject` 表示已知拒单，不自动重试。
- 成交按 `fill_id` 去重；完全重复返回 `None`，冲突拒绝且不覆盖原证据。部分成交可乱序抵达；累计不能超过订单数量。状态回调不生成 Fill。状态累计领先实际成交时，对账必须等待成交证据补齐。
- `cancel(order_id, authorized=True)` 要求字面量 `True`，包括终态订单；`1`、字符串不算授权。`inject_cancel_timeout()` 保持 PENDING_CANCEL；重复调用不会伪造取消成功，仍可能收到部分成交。断连不会隐式取消。
- 重复/旧状态不能让累计成交倒退或把 PENDING_CANCEL 回退为 SUBMITTED。确认取消前发生、之后迟到的成交可以入账；确认取消之后发生的成交拒绝。重复取消回调不能延长可接受成交时间窗。
- 新离线日志初始已对账。断连/重连、重启时仍有未终结订单或缺失成交证据，都需显式对账，不能恢复即发送。

### 持仓比对规则

`reconcile(account)` 不再仅检查现金/持仓非负：

**预期持仓 = 持久化 opening baseline + 每个唯一 BUY fill 数量 − 每个唯一 SELL fill 数量。**

会对比输入与推导结果的全部证券（忽略数量为零的条目）。少报、多报、缺失证券、额外证券均返回 `reconciled=False`、`POSITION_MISMATCH` 和逐证券 `expected/actual` 差异，同时阻断新单。输入必须是 reconciled 的 AccountSnapshot，UTC 时间不早于本地已有成交；现金、权益、成本和标价必须是有限非负 Decimal，持仓须为非负整数且 symbol/key 一致。

默认 opening baseline 是空持仓，**不会把第一次收到的外部持仓静默当作初始仓位**。如离线场景确实已有持仓，必须在全新日志第一次构造时显式指定：

```python
broker = SimulatedBroker(db_path, baseline_positions={'SPY': 20})
```

baseline 只表示该日志开始前的整股数量，持久化后不能修改；重启时可省略参数并使用已保存值。不能在出现差异后反向“调平”基线。既有无基线日志按空基线处理。负的推导持仓即便没有输入 account 也对账失败。

`reconcile(None)` 只检查本地日志/持仓推导，不声称核验了外部账户。**曾失败的账户对账会持久化 `account_check_required`，不能通过省略 account 或重启解除，必须提供修正后的快照。**

局限：baseline 不是外部认证；本模块不核算完整现金账本、现金流、保证金、成本法、拆股或股息。`reconcile(account)` 的现金/权益仅做有效性检查，不声称与完整现金流水吻合。公司行动、历史迁移和外部成交需上游显式处理；无法解释的持仓差异保持失败。模拟故障/成交价格及手续费都是软件测试输入，不是市场或费率证据。

## IBKR 查询恢复与未完成的对账

1. 等待 `nextValidId` 握手以及 `managedAccounts` 精确匹配配置账户后，才发送查询。账户仅用于过滤，不作为环境证明。
2. 每次 socket generation 仅启动一轮恢复：`reqAccountSummary`、`reqPositions`、`reqAllOpenOrders`、带账户过滤的 `reqExecutions`，另发时间心跳。请求 ID 与观察到的订单 ID 下界使用 SQLite FULL/WAL 持久化，重启不复用请求 ID；不分配或发送真实订单。
3. 必须收到对应 `accountSummaryEnd(reqId)`、`positionEnd()`、`openOrderEnd()`、`execDetailsEnd(reqId)`。无 request ID 的结束回调由 socket generation 隔离，旧连接回调、旧 reader 退出不能污染新连接。重复 summary end 不重复取消订阅；`cancelAccountSummary` 是查询订阅清理，不是订单撤单。
4. 要求三个 USD 账户字段齐全、心跳新鲜、无错误/持久化完整性标记、已知执行的手续费齐全，才标记 `recovery_snapshot_complete=True`。缺失手续费不会因重连或历史查询窗口缩短而消失。
5. 心跳超时、恢复回调缺失、陈旧心跳、局部/券商节流、连接/行情权限/拒单错误均报告并阻断恢复完成；没有自动重连/重试循环。显式重连也遵守本地请求限速；过低的限速可使首轮恢复失败，而不是绕过限制。
6. `1101`（恢复但数据丢失）和 `1102`（恢复且数据保留）在本版本均采取保守的清空 barrier、要求显式新连接恢复策略。该策略比官方对 1102 的要求更严格。错误只持久化数值代码和类别，不记录原始错误文本或完整账号。

**`recovery_snapshot_complete` 只表示本轮查询收齐，不等于跨查询原子快照、成交/订单/持仓已完成财务对账，更不等于 paper 验证。** `reconciled`、`can_submit` 始终为 False。传入外部 AccountSnapshot 不能替代结束回调或证明环境。

- `orderStatus` 可能重复；没有假定 IBKR 提供单调序列。保持累计不回退，状态本身不合成 Fill。不带账户的状态仅在订单/成交能关联账户时用于展示。
- `execDetails` 按 execId 持久化去重，部分成交各有独立 execId；冲突 ID、末段变化的执行更正、手续费更正持久化故障标记，需人工解释，不叠加为新持仓。
- 手续费可先于成交到达；缺失费用不补零。非 USD、非股票、非整股、未知买卖方向、待修订执行等不支持情形保持阻断。
- `fills()` 返回**原始证据字典**（`applied_to_ledger=False`），不是共享模型 Fill，也不是已核算收益。`broker_time` 原样保存，尚未可靠转换时区；不伪装成 UTC 账本时间。没有行情订阅或可用于下单的 market_price。
- `reqAllOpenOrders` 是一次性快照，不持续跟踪其他 client 的全部订单；不接管、不绑定、不全球取消。
- 当前官方执行查询有日内/有限历史限制；`execution_history_complete=False`。不能仅靠这轮查询重建初始持仓、完整历史账本或证明零遗漏；IBKR 的 baseline/账本整合和可信环境验证未实现。

## 官方文档证据

以下 URL 于 **2026-11-21 实际获取**（本轮约 17:30–17:47 UTC），使用可访问的 `interactivebrokers.com/docs/...` 官方镜像。任务提供的 `ibkrcampus.com` 403 限制没有作为绕过对象，也未将第三方博客当作权威。

| 官方来源 | 核实内容 / 对实现的影响 |
| --- | --- |
| [Requirements](https://interactivebrokers.com/docs/tws-api/doc/notes-limitations/requirements.md) | 原文：`Minimum supported Python release is version 3.11.0.`；TWS/Gateway 与 API 使用当前 Stable/Latest。离线项目支持 3.10 不代表真实适配器可用。 |
| [API settings](https://interactivebrokers.com/docs/tws-api/doc/api-settings.md) | 要核对实际 Socket Port；通用交易指南含关闭只读步骤，本任务不执行这些权限变更。没有从此页面推导端口可证明 paper。 |
| [Installation](https://interactivebrokers.com/docs/tws-api/doc/quick-start/installation.md) | 官方 TWS/API 下载与 Python 专门安装说明入口。 |
| [Python installation](https://interactivebrokers.com/docs/tws-api/doc/unique-configurations/updating-the-python-interpreter.md) | 从已经本地下载的官方包安装，并核对分发版本；没有执行安装或改写第三方依赖源码。 |
| [Establishing a connection](https://interactivebrokers.com/docs/tws-api/doc/connectivity/establishing-an-api-connection.md) | 版本握手、nextValidId；握手之前调用可能被丢弃。 |
| [Order status](https://interactivebrokers.com/docs/tws-api/doc/order-management/order-status.md) | 精确回调参数，重复消息，PendingCancel 不等于已取消且仍可能成交。 |
| [Execution details](https://interactivebrokers.com/docs/tws-api/doc/order-management/execution-details.md) | execDetailsEnd、每个部分成交独立 execId、执行更正末段变化；默认自午夜查询，TWS 配置可扩展至有限天数，Gateway 日内限制。 |
| [Place order](https://interactivebrokers.com/docs/tws-api/doc/orders/place-order.md) | `placeOrder(id, contract, order)` 是发送/修改；ID 需递增，不可把超时当成可安全新单重试。此版本不发送。 |
| [Cancel order](https://interactivebrokers.com/docs/tws-api/doc/orders/cancel-order.md) | 当前 Python 示例 `cancelOrder(orderId, OrderCancel())`；单单取消需原 client，global cancel 跨来源。此版本全部禁用。 |
| [Commission callback](https://interactivebrokers.com/docs/tws-api/doc/order-management/commission-and-fees-report.md) / [field reference](https://interactivebrokers.com/docs/tws-api/ref/commission-and-fees-report.md) | Python 名称 `commissionAndFeesReport`，字段参考为 `commissionAndFees`。叙述页还混有旧 `commissionReport`/`commission` 文案；按当前 Python 示例及字段参考实现，旧名称仅供离线 fixture 兼容，不据此认可旧 SDK。 |
| [Error callback](https://interactivebrokers.com/docs/tws-api/doc/error/messages.md) / [codes](https://interactivebrokers.com/docs/tws-api/doc/error/codes.md) | 当前 Python `error(reqId, errorTime, errorCode, errorString, advancedOrderRejectJson='')`；区分连接中断、恢复、节流及通知。不记录原始敏感文本。 |
| [Account summary](https://interactivebrokers.com/docs/tws-api/doc/accounts/summary.md) / [positions](https://interactivebrokers.com/docs/tws-api/doc/accounts/positions.md) | summary end、position end；summary 最多两个订阅，完成后清理，position 初始快照后有增量更新。 |
| [Open orders](https://interactivebrokers.com/docs/tws-api/doc/order-management/open-orders.md) | reqAllOpenOrders 一次性快照、openOrderEnd；client 0 绑定可能改变订单，故禁止接管。 |
| [Pacing](https://interactivebrokers.com/docs/tws-api/doc/pacing-limitations.md) | 限速与 market data lines 有关，默认示例为 50/s；本地 10/s 只是更保守的策略，不能证明外部配额。 |

文档核实不等于 SDK 二进制/协议兼容性验证；文档本身的旧新字段混用也已明确记录。没有实际 SDK、TWS、账号和授权，不能宣称 official adapter 联调通过。

## 验证方法与边界

执行模块测试（无外部依赖下载）：

```bash
.venv/bin/python -m pytest tests/broker
```

本模块测试用 `conftest.py` 禁止 socket connect/connect_ex/create_connection。`test_simulator.py` 覆盖持久化去重、timeout/reject/disconnect、部分/乱序/重复成交与状态、取消授权/超时、重连/重启、显式 baseline、真实持仓差异、输入无效、账户重检防绕过和并发去重。

`test_ibkr_mock.py` 中所有 transport、账户和官方类替身明确标记 **MOCK ONLY**，覆盖握手/查询 barrier、旧 generation 隔离、请求 ID 下界、手续费缺失/更正、节流、心跳、错误处理、环境拒绝、禁止订单变更、配置模板，以及实际本地 Python 3.10 的连接前拒绝。模拟 3.11 路径只测试本地逻辑；官方 EClient/EWrapper 替身测试仅验证组合/转发，不验证真实 ABI。

未验证：真实纸账户登录、官方 SDK 运行、TWS/API 版本协商、查询权限和实际回调时序、跨进程崩溃/断电恢复、完整历史与现金/持仓账本、可信 paper 认证、任何真实订单或撤单。未运行共享全量集成测试。以上缺项不能以 mock 结果替代。
