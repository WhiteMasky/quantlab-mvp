# QuantLab · 本地量化研究与模拟执行 MVP

**研究软件，不是可实盘系统。默认完全离线。** UI/API 仅绑定回环地址，没有公网部署、真实交易或账号权限操作。合成收益不是市场历史，更不是投资证据。

## 启动 Demo

已安装的本机环境可以直接运行两个终端：

```bash
bash scripts/dev.sh api
bash scripts/dev.sh web
```

打开 **http://127.0.0.1:15173**。API 为 http://127.0.0.1:18765。采用独立端口，避免打断本机其他应用。已启动时直接访问，不要重复启动第二个服务。

[桌面效果](reports/demo-final.png) · [移动端效果](reports/demo-mobile-final.png) · [真实验收记录](reports/ACCEPTANCE.md) · [策略研究报告](reports/RESEARCH.md)

1. 进入“策略研究”，保持 `trend / lookback=63 / seed=7`，运行离线实验，通常约 20 秒，慢机器更久。
2. 查看数据质量、净值/回撤、成本、训练/验证/滚动样本外/封存测试结果。
3. 查看“组合持仓”和“订单与成交”：这里是独立的历史合成回放账本，不是回测末期持仓，也不是 IBKR 成交。
4. 完全相同的重跑只回放已封存结果，不重复测试或买入。改参数后不能重新触碰同一数据/策略的测试集，失败实验仍记录。
5. 风控停止只阻止新单，独立通道不等待长回测；恢复需明确理由。撤单另行确认，不会清仓。无实盘入口。

历史曲线使用用户请求的参数；验证协议独立选择参数，须以验证详情为准，不把整段历史表现冒充样本外表现。

## 从源代码安装

离线功能要求 Python 3.10+；当前官方 IBKR API 要求 Python 3.11+。前端建议 Node 24。当前本机 `.venv` 是 Python 3.10.12，**不能进行官方 IBKR 连接**。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
npm --prefix frontend ci
```

本机额外使用 `.tools/node/bin` 下的 Node；启动脚本会优先使用它。`requirements.lock` 与 `frontend/package-lock.json` 保存本次依赖版本。安装时需要软件包网络访问，不会下载市场数据或安装 IBKR SDK。

## 命令行与本地数据

CLI 与 API 不要同时使用同一个 runtime；单个运行目录只运行一个服务进程。不要通过删除运行目录来消除失败实验或重新开启已查看测试集。

```bash
# 独立的可复现演示账本；JSON 输出要求新文件名，防止覆盖既有报告
.venv/bin/python -m quantlab --runtime runtime/cli-demo --output reports/my-trend.json demo --strategy trend --seed 7

# 用户自己提供的严格 CSV 或本项目 Parquet 缓存，仅研究，绝不下单
.venv/bin/python -m quantlab --runtime runtime/local-data --output reports/my-research.json research /absolute/path/prices.csv
# 如果 CSV 是合成夹具，必须显式加 --synthetic；Parquet 保留其自身标签
```

CSV 必需表头：

```text
symbol,session,open_at,close_at,available_at,open,high,low,close,adjusted_close,volume
```

可选公司行动四列必须整体提供：`ex_date,known_at,split_ratio,cash_dividend`。ISO 日期、明确 UTC 秒级时间、原始 OHLC、独立复权收盘、整数股数；不填补、不猜测、不静默丢行。拆股比例是新股/旧股，现金股息为每一拆股后应得股份的美元金额。详细格式见 `quantlab/data/sources.py`。

本地文件来源/公司行动完整性/历史股票池/退市与复权时间点均未经外部核验。导入研究的统计门槛通过也不是投资审批。Web 的运行按钮始终生成合成 Demo；本版没有网页上传真实数据功能。

## 实现结构

- `quantlab/data`：真实交易日历、严格 CSV、Decimal 无损 Parquet、内容哈希、质量诊断、合成夹具。
- `research` / `strategy`：失败也保留的 SQLite 实验库、预注册假设、确定性趋势与截面动量。
- `portfolio`：波动率权重、相关性约束、仓位/现金/换手限制、整股目标与稳定订单 ID。
- `backtest`：下一交易日原始开盘成交、费用/价差/滑点、此前已知成交量上限、DAY/GTC、部分成交、公司行动与共享账本。
- `validation`：按时间 40/20/10/10/20 切分；验证期选参后两段滚动样本外、2/3 倍成本和一次封存测试；Bootstrap 不确定性和阶段结果。
- `risk`：独立、持久化、逐单拒绝式风控，配置/高水位/日亏损/停止状态和审计。
- `broker`：可故障注入的持久化模拟器；官方 IBKR 查询适配器，环境未知则所有订单变更始终禁止。
- `engine` / `service`：总控执行网关、本地 FastAPI、请求审计、跨运行测试集封存、线程安全停止通道。
- `frontend`：六页面 React 控制台，全部数据来自实际 API；没有假收益占位。

接口和十个执行角色的职责见 `docs/CONTRACT.md`；实际 Agent 执行与中断恢复记录见 `reports/agent-execution.json`。每个执行模块都有自己的单元测试，总控补集成测试，不把全部测试转交产品角色。

## IBKR：已做与没做

参见 **[docs/IBKR.md](docs/IBKR.md)** 和非秘密模板 `config.ibkr.example.json`。

- 默认禁用、没有自动连接。完整账号只能保留在本地 `config.ibkr.local.json`（已忽略），不要传给前端或提交。
- 将来在独立授权下核对本机官方 SDK/TWS 版本、实际 socket 端口、明确纸账户以及 Python 3.11+，保持只读，才可显式使用查询适配器。
- 已实现握手、心跳、限速、查询恢复 barrier 和去重等本地逻辑；测试使用 MOCK，未实连券商。
- DU 前缀、PAPER 配置或某端口都不是可信环境认证。**本版本的 IBKR 下单和撤单永久阻断**，不能通过配置解锁。
- 可信 paper 环境认证、完整外部资金/持仓对账、真实 SDK/TWS 联调、行情权限等尚未完成；不宣称纸盘交易可用。

## 测试与验收

```bash
.venv/bin/python -m pytest
PATH="$PWD/.tools/node/bin:$PATH" npm --prefix frontend test
PATH="$PWD/.tools/node/bin:$PATH" npm --prefix frontend run build
```

具体实际执行次数、浏览器验收和问题见 `reports/ACCEPTANCE.md`。使用 Wuying 桌面时，重型测试/构建通过 `/usr/local/bin/wybuild -- ...` 隔离执行。

`scripts/browser-demo.mjs` 使用已安装的 Google Chrome（默认 `/usr/bin/google-chrome`，可通过 `CHROME_PATH` 指定），不下载浏览器。它会实际产生离线实验、模拟成交和风控审计，并要求空的初始账本；不要针对已交付 Demo 直接重跑、删除账本或覆盖既有报告。日常查看不需要执行该脚本。

## 不应被忽略的限制

合成行情固定自 2022-01-03 起 504 个 XNYS 交易日，四只 ETF 标签不是实际历史报价；包含虚构股息和拆股，复权是回溯计算，不是 PIT 数据。成本假设为每股 $0.005、最低 $1、全价差 2bps、滑点 5bps，并非当前 IBKR 费率。没有税务、T+1 结算现金限制、借贷、公司行动支付日精确现金流或开盘订单簿模型。只支持多头整股股票/ETF、日频研究。

重新开启实盘前至少必须完成：新的明确授权、独立代码/安全审查、可信账户环境识别、官方 SDK/TWS 纸盘端到端测试、完整资金/持仓/执行对账及灾难恢复、真实 PIT 数据和不接触测试集的研究验证、真实费率/结算/公司行动建模、监控/告警/运维与操作审批。盈利无法保证，本 MVP 不具备生产交易条件。

## 界面参考

参考公开产品的信息组织而非复制收益或暗示功能等同：

- [QuantConnect 回测结果](https://www.quantconnect.com/docs/v2/local-platform/backtesting/results)：净值、基准、回撤、实验与交易记录。
- [IBKR Risk Navigator](https://www.interactivebrokers.com/campus/trading-lessons/introduction-to-ibkrs-risk-navigator/)：风险敞口与独立风险观察。
