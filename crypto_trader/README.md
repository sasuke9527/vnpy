# crypto_trader — 50 USDT 永续合约趋势跟踪系统（vnpy 4.4.0）

基于 vnpy 4.4.0 / vnpy_ctastrategy 1.4.1 / vnpy_binance 2026.8.5 / vnpy_okx 2026.6.10 的无界面（headless）实盘程序。
两个 CTA 策略、一个共享的风险守卫、一个数据下载器、一个回测/验收脚本和一个带看门狗的实盘运行器。
所有代码标识符为英文，本文档为中文。

> 先读 [目的与诚实的预期收益](#1-目的与诚实的预期收益)。这个系统不会把 50 USDT 在一个月内变成 300 USDT，设计上也拒绝任何能做到这一点的仓位。

---

## 1. 目的与诚实的预期收益

目标：用 50 USDT 的保证金，在 Binance USDT-M 永续（默认 ETHUSDT）或 OKX 永续上，
以严格的风险控制运行一个 1 小时级别的 Donchian 趋势突破策略（S1），可选地再加一个 15 分钟级别的
挤压突破策略（S2，默认关闭，需先通过第 7 节的验收门槛）。

设计文档第 9 节给出的、经过手续费和资金费率之后的**诚实预期**：

| 旋钮 | 每笔风险 | 月交易数 | 期望收益 | 月度波动 σ | 备注 |
|---|---|---|---|---|---|
| `normal` | ≈ 1 USDT (2 %) | 5–10 | **+1…3 USDT/月（+2…6 %）**，期望 +0.15…0.30 R | ≈ 10 % | 震荡市里 4–6 连败很常见，−5…−8 % 的月份是正常现象 |
| `aggressive` | ≈ 2 USDT (4 %) | 5–10 | ≈ +5…12 %/月 | ≈ 20 % | 单月回撤 > 30 % 的概率 ≈ 15–25 % |

- 30 天内到 300 USDT（+500 %）需要在 ~5× 杠杆下从头到尾吃到一段 +30–40 % 的 ETH/山寨币趋势且一次都不止损。
  在本设计允许的任何旋钮下，P(30 天到 300) 远低于 1 %；能让它成为可能的仓位方式（10× 以上名义仓位、不设止损）正是本设计拒绝的东西。
- 现实路径：3 个月 50 → 60–80 USDT；如果边际在样本外得到验证，`aggressive` 旋钮下 9–12 个月到 100–150 USDT。
- 任何"第一个月就要赚回订阅费"的规则几乎肯定无法满足，本文档不假装能满足。

---

## 2. 架构一览

```
crypto_trader/
  .vntrader/                 # vnpy 的状态目录（运行时自动创建；所有脚本以 crypto_trader 为 cwd）
    vt_setting.json          #   {"database.timezone": "UTC", "log.file": true}
    database.db              #   1m K 线（sqlite）
    cta_strategy_setting.json#   已部署策略及其参数
    cta_strategy_data.json   #   策略持久化变量（pos、止损价、风控计数……），重启后恢复
    risk_state.json          #   账户级共享风控状态（日初权益、权益峰值、symbol 锁、halted）
    exchange_filters.json    #   交易所过滤器（tickSize/stepSize/minQty/minNotional），run_live.py 每次启动刷新
    funding_<SYMBOL>.json    #   资金费率历史（回测用）
    heartbeat_<name>         #   策略心跳（epoch 秒），run_live.py 看门狗读取
    KILL / PAUSE / RESUME    #   开关文件，见第 11 节
    log/vt_YYYYMMDD.log      #   日志
  settings.py                # DIALS 风险旋钮表、FEES、DEPLOYMENT 部署清单、.env 读取、网关 setting、symbol 工具
  sizing.py                  # 仓位计算：floor_to/ceil_to (Decimal)、fee_rt()、min_stop_pct()、calc_volume()、LotInfo
  risk.py                    # RiskGuard（每策略风控对象）+ SharedRiskState（账户级共享状态）、reconcile、heartbeat
  strategies/
    donchian_trend_h1.py     # class DonchianTrendH1(CtaTemplate)  — S1，默认开启
    squeeze_break_15m.py     # class SqueezeBreak15M(CtaTemplate)  — S2，默认关闭
  download_data.py           # 公共 REST：Binance /fapi/v1/klines、fundingRate、exchangeInfo；OKX history-candles、instruments
  binance_vision.py          # data.binance.vision 归档批量下载（fapi 被 451 封锁时用）
  synth_data.py              # 合成 1m K 线（离线测试用）
  backtest.py                # 回测：手续费三档、资金费率后处理、3×3×3 参数网格、验收报告
  run_live.py                # 实盘运行器：过滤器刷新 → 网关 → 孤儿撤单 → init/start → 看门狗 → 心跳
  requirements.txt           # 版本锁定
  .env.example               # 环境变量模板（复制为 .env，绝不提交）
  tests/                     # pytest；tests/conftest.py 在导入 vnpy 之前把 cwd 切到临时目录，测试不碰你的状态文件
```

关键约定：

- **cwd 必须是 `crypto_trader/`**。vnpy 在首次 import 时把 `TRADER_DIR` 定为 `cwd/.vntrader`（若存在）否则 `~/.vntrader`。
  `run_live.py`、`download_data.py`、`backtest.py` 都会自己 `os.chdir` 到项目目录并创建 `.vntrader`，所以你可以从任何地方启动它们。
- 两个网关都使用 `Exchange.GLOBAL`：Binance 的 vt_symbol 是 `ETHUSDT_SWAP_BINANCE.GLOBAL`，OKX 是 `ETHUSDT_SWAP_OKX.GLOBAL`。
  `settings.deployment_for(exchange)` 会按 `CT_EXCHANGE` 自动改写 `DEPLOYMENT` 里的 vt_symbol。
- 策略文件放在 `strategies/`，由 `CtaEngine.load_strategy_class` 从 `cwd/strategies` 自动发现，按类名注册（`DonchianTrendH1`、`SqueezeBreak15M`）。

---

## 3. 部署步骤

### 3.1 macOS

```bash
# 1. Python 3.11（Homebrew）
brew install python@3.11
# 2. 取得代码，进入项目目录
cd /path/to/crypto_trader
# 3. 虚拟环境与依赖
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
# TA-Lib：vnpy 依赖 ta-lib。若 pip 找不到匹配的 wheel 而编译失败，先装 C 库再重装：
#   brew install ta-lib && pip install --no-cache-dir ta-lib
# 4. 配置
cp .env.example .env && chmod 600 .env   # 然后填入密钥
# 5. 离线自检（不联网）
QT_QPA_PLATFORM=offscreen python run_live.py --dry-run
```

### 3.2 Linux VPS（Ubuntu 22.04 / Debian 12）

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv build-essential chrony
sudo systemctl enable --now chrony          # 时钟必须 NTP 同步（Binance 下单签名用本机时间，偏差 > 5 s 会被 -1021 拒绝）
sudo useradd -m -s /bin/bash trader
sudo -iu trader
git clone <your-repo> ~/crypto_trader        # 或 scp 上传
cd ~/crypto_trader
python3.11 -m venv .venv && source .venv/bin/activate
pip install -U pip && pip install -r requirements.txt
# 若 ta-lib 编译失败：从 https://github.com/ta-lib/ta-lib/releases 安装对应的 .deb（ta-lib_0.6.x_amd64.deb），再 pip install ta-lib
cp .env.example .env && chmod 600 .env
QT_QPA_PLATFORM=offscreen python run_live.py --dry-run
```

`QT_QPA_PLATFORM=offscreen`：vnpy 的部分模块会导入 Qt；在没有显示器的机器上设置这个变量以避免 Qt 平台插件报错。
所有命令、systemd 单元和 launchd plist 里都带上它。

---

## 4. 交易所准备（网关不会替你设置任何东西）

### 4.1 Binance USDT-M 永续（`CT_EXCHANGE=binance_linear`）

1. 开通 **USDT-M 合约**账户，划转 50 USDT 到合约钱包。
2. 持仓模式设为**单向持仓（One-way）**。对冲模式（Hedge）下网关发出的订单没有 `positionSide`，会被 `-4061` 拒绝，且仓位推送被忽略。
   实盘网关是 `gateways.ReduceOnlyBinanceLinearGateway`（`vnpy_binance` 的子类）：所有平仓单（止损子单、限价追单）都带 `reduceOnly=true`，
   因此低于 `MIN_NOTIONAL`（ETHUSDT 20 USDT）的残余仓位也能平掉（`-4164` 只针对非 reduce-only 单），而且平仓单永远不会把仓位反向开出来。
   `reduceOnly` 只有单向持仓模式接受——这是必须用 One-way 的另一个原因。
3. 保证金模式设为**全仓（Cross）**。
4. 手动把交易品种（ETHUSDT）的**杠杆设为 10×**。这不是策略杠杆——策略自己限制名义仓位 ≤ `max_leverage`×余额（normal 旋钮 3×）；
   交易所杠杆设为 ≥ 2×`max_leverage` 只是为了初始保证金永远不会拒单。
5. API Key：只勾选 **Enable Futures** 和读取权限；**不要**开启提现；**限制 IP 白名单**为 VPS 的出口 IP。
6. `settings.gateway_setting()` 自动设置 `"Kline Stream": "True"`，策略在实盘用交易所推送的已收盘 1m K 线（`tick.extra["bar"]`），不用本地 tick 合成。
7. 若你的网络访问 `fapi.binance.com` 返回 HTTP 451（地区封锁），配置 `CT_PROXY_HOST/CT_PROXY_PORT`（HTTP 代理，网关和下载器都会用）。

### 4.2 OKX 永续（`CT_EXCHANGE=okx`）

1. 账户模式至少为**现货和合约模式（Spot and futures mode）**，持仓模式为**买卖模式（net / one-way）**。网关固定 `tdMode="cross"`（全仓），不支持逐仓。
2. API Key 勾选 **Trade**（交易）权限，不要提现；设置 IP 白名单。
3. **模拟盘（DEMO）需要单独申请模拟盘 API Key**，与实盘 Key 不通用。`OKX_SERVER=DEMO` 或 `run_live.py --paper` 使用模拟盘。
4. **合约面值警告**：OKX 永续以"张"为单位，`contract.size = ctVal`。ETH-USDT-SWAP 一张 = 0.1 ETH（≈ 300–400 USDT），BTC-USDT-SWAP 一张 = 0.01 BTC。
   对 50 USDT 账户，策略在启动时检查 `lots_max = floor(余额×max_leverage ÷ 单张名义)`，**小于 4 就以 `COARSE_LOTS` 拒绝该品种并永久停用**（RESUME 也不会清除）。
   因此在 OKX 上默认的 ETHUSDT 部署会被自动拒绝；要在 OKX 交易，请把 `settings.DEPLOYMENT` 改成单张名义小的品种（XRP/DOGE 一类的 ctVal），并先跑通第 7 节的回测验收。
5. 同样需要 NTP 时钟同步。

---

## 5. 环境变量（`.env`）

```bash
cp .env.example .env
chmod 600 .env
```

`settings.load_env()` 读取顺序：内置默认值 ← `crypto_trader/.env` ← 进程环境变量（环境变量优先）。
支持 `KEY=VALUE`、`#` 注释、引号和行尾注释。**永远不要提交 `.env`。** `run_live.py --env PATH` 可以指定另一个文件（例如给模拟盘用 `.env.paper`）。

| 变量 | 取值 | 说明 |
|---|---|---|
| `CT_EXCHANGE` | `binance_linear` \| `okx` | 50 USDT 在哪个交易所 |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | | Binance 合约 API |
| `BINANCE_SERVER` | `REAL` \| `TESTNET` | `--paper` 强制 `TESTNET` |
| `OKX_API_KEY` / `OKX_SECRET_KEY` / `OKX_PASSPHRASE` | | OKX API（模拟盘用模拟盘 Key） |
| `OKX_SERVER` | `REAL` \| `DEMO` | `--paper` 强制 `DEMO`（旧值 `TEST` 会被映射为 `DEMO`） |
| `CT_PROXY_HOST` / `CT_PROXY_PORT` | 主机、端口 | 可选 HTTP 代理；网关和下载器共用 |
| `CT_RISK_DIAL` | `conservative` \| `normal` \| `aggressive` | 风险旋钮，写入每个策略的 `risk_dial` 参数 |
| `CT_EQUITY_USDT` | 数字，默认 50 | 策略参数 `capital`：启动竞态时钱包余额尚未到达前的权益回退值；回测本金 |

`run_live.py --dry-run` 会打印解析后的网关 setting（密钥只显示"已设置，N 个字符"），并在变量缺失或非法时以退出码 1 失败。

---

## 6. 数据下载

回测需要 1m K 线（存入 `.vntrader/database.db`）、资金费率（`.vntrader/funding_<SYMBOL>.json`）和交易所过滤器（`.vntrader/exchange_filters.json`）。全部来自公共 REST，不需要 API Key。

```bash
# Binance ETHUSDT 永续，1m K 线 + 资金费率 + 过滤器，2023-01-01 至今
python download_data.py --exchange binance_linear --symbol ETHUSDT --interval 1m --start 2023-01-01 --funding --filters
# 只刷新过滤器（run_live.py 每次启动也会自动刷新；失败时保留旧文件）
python download_data.py --exchange binance_linear --filters --skip-bars
# OKX
python download_data.py --exchange okx --symbol ETHUSDT --interval 1m --start 2024-01-01 --funding --filters
```

其他参数：`--end YYYY-MM-DD`（含当天）、`--no-save`（只拉取不写入）、`-v`。symbol 接受 `ETHUSDT`、`ETH-USDT-SWAP`、`ETHUSDT_SWAP_BINANCE` 等写法。
下载器会按交易所权重限制自动限速，遇到 429/418 会按 `Retry-After` 等待。

- `fapi.binance.com` 被 451 封锁时：`python binance_vision.py --symbol ETHUSDT --start 2023-01 --funding`（从 data.binance.vision 归档下载，压缩包缓存在 `data/vision/`）。
- 离线测试/演示：`python synth_data.py --symbol ETHUSDT_SWAP_BINANCE --days 60 --seed 1` 生成合成 K 线写入数据库（**不要**用合成数据做验收）。

---

## 7. 回测与验收门槛

`backtest.py` 用 `BacktestingEngine(interval=Interval.MINUTE)` 跑单个策略/品种/区间，做三档手续费（`rate ∈ {0.0004, 0.0005, 0.0007}`），
从 `engine.get_all_trades()` 回推每 8 小时（00/08/16 UTC）的资金费率成本（无资金费率文件时用 0.0001/8h，压力测试 ×2），
再在 fit（2023–2024）/ validate（2025）/ test（2026 至今）上评估，并对每个策略仅有的三个可调参数做 3×3×3 网格。参数用法见 `backtest.py` 文件头部说明。
回放中途异常终止或策略记录了异常的回测：验收表第一行直接 FAIL，退出码 3，网格中该格的目标值记为 `-inf`（不参与排名）——截断的结果永远不算通过。
R 期望值与"去掉最好的 5 % 交易"都按引擎的滑点（每个来回 `2 × 数量 × slippage`）扣减，与引擎的 `total_net_pnl` 口径一致。

**在投入任何实盘资金之前，每个策略、每个品种都必须在 `normal` 旋钮、`rate=0.0005`、扣除资金费率后满足全部条件：**

| 条件 | 门槛 |
|---|---|
| 期望收益（以 R = 入场时的风险金额计） | ≥ 0.15 R |
| Sharpe（年化 365 天） | ≥ 1.0，**validate 和 test 都要** |
| 最大回撤 | `max_ddpercent ≥ −25 %` |
| 交易数 | ≥ 100（S1 覆盖 ≥ 24 个月，S2 ≥ 12 个月） |
| 稳健性 | 去掉盈利最大的 5 % 交易后仍为正 |
| 参数稳定性 | 网格中位数 Sharpe ≥ 最优格的 70 % |
| 成本压力 | `rate=0.0007` 与 2× 资金费率两个压力档均为正 |
| S2 附加 | 以山寨币滑点 `slippage=0.0008×价格` 计算，期望 ≥ 0.15 R，否则保持关闭 |

只有通过后才把 `settings.DEPLOYMENT` 里对应的行取消注释（S2 默认注释掉）。S2 永远不要和 S1 部署在同一个品种上（symbol 锁会拒绝第二个策略入场）。

离线单元测试（不联网，使用临时目录，不碰你的状态文件）：

```bash
QT_QPA_PLATFORM=offscreen python -m pytest tests -q
```

---

## 8. 试运行（测试网 / PAUSE 影子模式）

回测通过之后，至少 **3 天**影子运行，把日志里的指标值和回测在相同时间戳的值对比：

```bash
# 方式 A：交易所测试网（Binance TESTNET / OKX DEMO；OKX 需要模拟盘 Key）
QT_QPA_PLATFORM=offscreen python run_live.py --paper
# 方式 B：真实账户 + PAUSE 文件（只看不做：不开新仓，已有仓位的止损/退出照常工作）
touch .vntrader/PAUSE
QT_QPA_PLATFORM=offscreen python run_live.py
```

- S1 在实盘每根 1h K 线收盘时写一行 `1h <时间> c=… ef=… es=… atr=… adx=… dc=…/… L=… S=…`（EMA 快/慢、ATR、ADX、Donchian 通道、多空信号），用它和回测对照。
- 建议把模拟盘跑在**另一份项目目录副本**里：`.vntrader/cta_strategy_data.json`、`risk_state.json` 等状态文件按目录隔离，不要让测试网的状态和实盘混在一起。
- 影子期结束、准备实盘时删除 `PAUSE`（`rm .vntrader/PAUSE`），策略在下一次风控检查（≤ 5 秒）后恢复入场。

---

## 9. 实盘运行与守护

```bash
cd /path/to/crypto_trader
QT_QPA_PLATFORM=offscreen .venv/bin/python run_live.py            # 前台
```

`run_live.py` 的启动流程：刷新 `exchange_filters.json` → 创建 `MainEngine` + 网关 + `CtaStrategyApp` → `connect` →
等待合约信息（≤ 120 s，OKX 再等 3 s 让 WebSocket 连上）→ **等待 `<网关>.USDT` 账户快照**（≤ 60 s，没有则退出码 4：仓位对账在账户数据到达前不会执行）→
`cta.init_engine()`（重新加载已保存的策略）→ 补齐 `DEPLOYMENT` 里缺少的策略、按 `.env` 刷新旋钮 →
**撤销部署品种上的所有孤儿挂单**（上一进程留下的挂单成交后不会进入 `pos`；本地止损单已随进程消失）→ 每个策略 `init_strategy(...).result(timeout=600)` 后立刻 `start_strategy` →
看门狗循环。

看门狗（每 10 s）：

- 策略 `trading` 变为 False（引擎在回调异常时同时清掉 `inited` 和 `trading`）→ **先撤销引擎里仍登记在该策略名下的所有委托（包括本地止损单，引擎异常停机时不会撤单）**，
  再 `init_strategy` + `start_strategy`（状态已持久化，幂等）；第一笔 tick 重新挂唯一的一张保护止损。预热失败（`load_bar` 异常、指标为空）的策略不会被启动，而是等下一轮重试。
  每个策略每滚动小时最多 3 次；第 4 次写入 `.vntrader/KILL`、`main_engine.send_notification`，**退出码 2**。
- `heartbeat_<name>` 超过 180 s 没更新（策略每 10 s 的 tick 写一次）→ **退出码 3**，让 systemd/launchd 重启进程。
- SIGINT/SIGTERM → 停止策略（撤销该策略的所有挂单、持久化变量）、关闭网关、退出码 0。

退出码：0 正常 / 1 配置错误 / 2 看门狗放弃（已写 KILL）/ 3 心跳过期 / 4 合约未到达或策略初始化失败。
配合 `Restart=always`：进程被重启后如果 `KILL` 存在，策略会启动、平仓、停机，直到你创建 `RESUME`。

其他参数：`--dry-run`（不联网自检）、`--paper`、`--env PATH`、`--no-filters`（跳过过滤器刷新）。

### 9.1 systemd（Linux VPS）

`/etc/systemd/system/crypto_trader.service`：

```ini
[Unit]
Description=crypto_trader live runner (vnpy)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/home/trader/crypto_trader
Environment=QT_QPA_PLATFORM=offscreen
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/trader/crypto_trader/.venv/bin/python /home/trader/crypto_trader/run_live.py
Restart=always
RestartSec=15
KillSignal=SIGTERM
TimeoutStopSec=90

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now crypto_trader
sudo systemctl status crypto_trader
journalctl -u crypto_trader -f                 # 控制台日志；文件日志在 .vntrader/log/
```

### 9.2 launchd（macOS）

`~/Library/LaunchAgents/com.crypto_trader.live.plist`（把路径换成你的）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.crypto_trader.live</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/crypto_trader/.venv/bin/python</string>
    <string>/Users/you/crypto_trader/run_live.py</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/you/crypto_trader</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>QT_QPA_PLATFORM</key><string>offscreen</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>/Users/you/crypto_trader/.vntrader/log/launchd.out.log</string>
  <key>StandardErrorPath</key><string>/Users/you/crypto_trader/.vntrader/log/launchd.err.log</string>
</dict>
</plist>
```

```bash
launchctl load -w ~/Library/LaunchAgents/com.crypto_trader.live.plist
launchctl list | grep crypto_trader
launchctl unload ~/Library/LaunchAgents/com.crypto_trader.live.plist   # 停止
```

Mac 需关闭睡眠（`sudo pmset -a sleep 0 disablesleep 1`）或者干脆用 VPS；笔记本合盖断网会触发心跳过期重启。

---

## 10. 风险旋钮（`settings.DIALS`）

`CT_RISK_DIAL` 决定每个策略的 `risk_dial` 参数；策略在 `on_init` 时用下表覆盖自己的风险参数（`risk_dial="custom"` 时保留策略文件里的显式值）。

| 键 | conservative | normal | aggressive | 含义 |
|---|---|---|---|---|
| `risk_pct_s1` | 1.0 % | 2.0 % | 4.0 % | S1 每笔止损亏损占余额比例 |
| `risk_pct_s2` | 0.75 % | 1.5 % | 3.0 % | S2 同上 |
| `max_leverage` | 1.5× | 3× | 5× | 单策略名义仓位 ≤ 余额 × 此值 |
| `gross_leverage` | 2× | 4× | 6× | 账户总名义仓位上限（所有策略之和） |
| `daily_loss_pct` | 4 % | 6 % | 10 % | 当日权益（含浮盈亏）回撤到此值：当天所有策略禁止新开仓，浮亏仓位平掉，盈利仓位保留移动止损 |
| `max_dd_halt` | 15 % | 25 % | 35 % | 权益从峰值回撤到此值：全部平仓并停机，需要手动 `RESUME` |
| `max_consec_losses` | 3 | 4 | 4 | 连续亏损次数达到后进入冷却 |
| `cooldown_hours` | 12 h | 8 h | 6 h | 冷却时长 |
| `max_trades_day_s1` / `_s2` | 2 / 4 | 3 / 6 | 3 / 6 | 每日最大交易次数 |

所有旋钮共有的硬规则：

- 仓位 = min(风险名义、杠杆名义、账户剩余名义)，向下取整到交易所步长；不足最小名义（ETHUSDT 20 USDT）时只允许在 `lot_tolerance=1.5` 倍以内向上凑整，否则放弃这笔交易；任何路径下止损亏损都不会超过 2× 旋钮风险。
- 止损距离 ≥ max(0.4 %, 3× 往返成本)；连败后仓位按 `0.5^(n−2)`（最低 0.25）自动缩减。
- 每分钟 ≤ 6 笔委托；连续 3 次被拒 → `REJECTS` 停机。
- 强平说明：全仓、总名义 ≤ 6× 时，大约 15 % 的不利行情才会强平；每笔止损（≈ 2 %）和日亏损（≤ 10 %）在任何旋钮下都远早于它触发。交易所 10× 杠杆设置只是为了初始保证金永远不拒单。

---

## 11. 开关文件（`.vntrader/` 下的空文件）

策略每根管理 K 线和每 5 秒的 tick 检查一次这些文件（`risk.RiskGuard.check_flags`）。**只有实盘读取这些文件**：
回测（`backtest.py`、网格 worker、pytest）既不响应也不删除它们，所以影子阶段（`PAUSE` 存在时）照常可以跑回测。

| 文件 | 作用 |
|---|---|
| `KILL` | 撤销入场挂单，用限价追单平掉所有仓位（直到平完，绝不放弃），账户级 `halted=True/"KILL"`。看门狗放弃时也会自动写入。 |
| `PAUSE` | 不开新仓（撤销入场挂单）；已有仓位的止损、移动止损、退出照常。删除文件即恢复。 |
| `RESUME` | 清除原因为 `REJECTS` / `DRAWDOWN` / `KILL` / `EXCEPTION` / `DESYNC` 的停机（回撤停机同时重置权益峰值）；处理后 `RESUME` 和 `KILL` 文件都会被删除。 |

```bash
touch .vntrader/KILL      # 紧急平仓并停机
touch .vntrader/PAUSE     # 只看不开新仓
touch .vntrader/RESUME    # 清除停机、删除 KILL
```

不会被 `RESUME` 清除的：`COARSE_LOTS`（品种对账户太粗，永久拒绝）；`DAILY_LOSS` 在下一个 UTC 日自动清除。

---

## 12. 状态恢复与重启行为

- `cta_strategy_data.json` 在每次成交、`sync_data()` 和停止时写入；重启后 `init_strategy` 在 `on_init` **之后**恢复 `pos` 和全部 `variables`（权益、入场价、止损价、最高/最低价、日内计数、停机原因……）。
- 委托号（`entry_orderid`/`stop_orderid`/`exit_orderid`）在重启后视为失效，`on_start` 直接清空；`run_live.py` 已把交易所上的孤儿挂单撤掉。
- 第一笔 tick 到达时（`on_tick` 首次调用）：
  1. **对账**：读取 `main_engine.get_all_positions()` 的净持仓，与本地 `pos` 比较（容差半个步长）。
     只有一个策略在该品种上 → 采用交易所数值（`adopt`：`entry_price` 取交易所均价，止损按当前 ATR 重算；`clear`：交易所已平，清空交易状态）。
     多个策略在同一品种且不一致 → `halted=True/"DESYNC"` + 通知，不自动下单。**永远不会为了"修正"数量而发单。**
  2. 用最新价刷新 `highest/lowest_since_entry`，若止损价缺失则按 `entry ∓ stop_mult×ATR` 重算，**立即重新挂本地止损单**。
  3. 检查开关文件，`sync_data()`。
  无保护窗口只有几秒。宕机期间的成交在本地是未知的，由 adopt/clear 覆盖；实盘权益本来就来自钱包余额。
- 此后每 60 s 再对账一次（S2 在每次成交 5 s 后额外强制对账一次）。
- 停止（SIGTERM/`Ctrl-C`）时引擎会撤销该策略的所有挂单（包括本地止损单）——**进程不在时仓位没有止损保护**，重启后第一笔 tick 会重新挂上。长时间停机前请手动平仓或 `touch KILL`。
- 启动时 `KILL` 存在 → 策略启动、平仓、停机；`RESUME` 清除。

---

## 13. 监控

| 看什么 | 在哪里 |
|---|---|
| 日志 | 控制台 + `.vntrader/log/vt_YYYYMMDD.log`（loguru，含网关、CTA 引擎、策略 `[策略名]` 前缀、`run_live` 行） |
| 心跳 | `.vntrader/heartbeat_<策略名>`：epoch 秒，每 10 s 由 tick 更新；`run_live.py` 超过 180 s 未更新即退出 3 |
| 风控状态 | `.vntrader/risk_state.json`：`day_key`、`day_start_equity`、`peak_equity`、`halted`/`halt_reason`、`locks`、`open_notional` |
| 策略状态 | `.vntrader/cta_strategy_data.json`：`pos`、`entry_price`、`stop_price`、`consec_losses`、`trades_today`、`halted`、`halt_reason` |
| 通知 | `main_engine.send_notification` → 邮件（在 `.vntrader/vt_setting.json` 配置 `email.server/port/username/password/sender/receiver`）和/或企业微信（`wechat_setting.json`）。未配置时只写日志。 |

值得设告警的日志关键字：`HALT`、`CRITICAL`、`PANIC`、`watchdog`、`reconcile`、`order rejected`、`触发异常已停止`。

```bash
tail -f .vntrader/log/vt_$(date -u +%Y%m%d).log | grep -E "HALT|CRITICAL|PANIC|watchdog|filled|closed"
```

---

## 14. 常见问题

**`contracts not received within 120s`（退出码 4）** — 网关没拿到合约列表：网络/代理不通、Binance 451 地区封锁、OKX 四个 instType 列表中有一个失败。检查 `CT_PROXY_*`，看日志里 REST 的报错。

**下单被拒 `-4061`** — Binance 账户是对冲模式，改成单向持仓。**`-2019` 保证金不足** — 品种杠杆太低，设为 10×。**`-1021` 时间戳** — 机器时钟不同步，开启 NTP（chrony/timesyncd）。**`-4164` 名义不足** — 正常现象，说明 `exchange_filters.json` 过期；`run_live.py` 每次启动会刷新，也可手动 `download_data.py --filters --skip-bars`。

**策略日志 `lot_notional … lots_max=N < 4; refusing symbol` / `halt_reason=COARSE_LOTS`** — 这个品种最小一手对你的账户太大（OKX ETH/BTC、Binance BTCUSDT 都会触发）。换品种或加资金；`RESUME` 不会清除。

**`HALT REJECTS`** — 连续 3 次委托被拒（通常是账户设置问题）。修好原因后 `touch .vntrader/RESUME`。

**`HALT DRAWDOWN` / `HALT DAILY_LOSS`** — 按设计停机。`DAILY_LOSS` 次日 UTC 自动解除；`DRAWDOWN` 需要你审视后 `RESUME`（同时重置峰值）。

**`HALT DESYNC`** — 同一品种上多个策略且本地仓位与交易所不一致。手动核对交易所仓位，必要时手动平仓、修改 `cta_strategy_data.json` 中的 `pos`，再 `RESUME`。

**看门狗反复重启、最后写了 KILL（退出码 2）** — 看 `.vntrader/log/` 里的 `触发异常已停止` traceback，修复后 `RESUME` 并重启服务。

**实盘的 1h 指标值和回测对不上** — 确认数据库时区是 UTC（`vt_setting.json` 的 `database.timezone`），确认实盘用的是 K 线流（Binance）；OKX 用 tick 合成 1m K 线，成交量不可靠但本策略不使用成交量。

**能不能把 `risk_pct` 调到 10 %、杠杆调到 20×？** — 不能。`calc_volume` 的绝对上限（止损亏损 ≤ 2× 旋钮风险）、`check_order` 的 `0.98×余额×max_leverage` 上限和 `COARSE_LOTS` 检查都会拒绝；这正是第 1 节说的"设计拒绝的仓位"。

**如何换到 OKX？** — `.env` 里 `CT_EXCHANGE=okx` 并填 OKX Key；`DEPLOYMENT` 的 vt_symbol 会自动改写为 `…_SWAP_OKX.GLOBAL`。如果 `cta_strategy_setting.json` 里还保存着 Binance 的旧条目，`run_live.py` 会报错要求先删掉它（引擎不能给已有策略改品种）。注意第 4.2 节的合约面值警告。

**`.env` 会被读到哪里？** — 只有 `crypto_trader/.env`（或 `--env PATH`）；文件里的每一行 `KEY=VALUE` 都会读取，进程环境变量只覆盖 `settings.ENV_KEYS` 里列出的键（以及文件里出现过的键），且环境变量优先。文件里的密钥不会出现在任何日志里（`--dry-run` 只打印长度）。
