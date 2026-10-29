# 天府退休计划 v1.5

独立 Flask + 原生 JS 的 **日终报价模拟** 系统。2026-01-01 建账，以 250,000 美元启动；首个 NYSE 交易日买入 BOXX。默认服务 `http://localhost:10020/#overview`。macOS 普通用户无法绑定原计划的 1002 特权端口。

## 启动

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 manage.py backfill --start 2025-01-01
python3 run.py
```

日常可直接运行 `sh run.sh`；macOS 可双击 `启动天府退休计划.command`。启动器参考原项目的“一键运行并打开浏览器”流程，依赖缺失时才安装，并打开 `http://localhost:10020/#overview`。

项目文档：[策略规范](STRATEGY.md) · [架构](ARCHITECTURE.md) · [页面设计](DESIGN.md) · [技术说明](TECHNICAL.md) · [数据源核验](DATA_SOURCES.md) · [变更记录](CHANGELOG.md)。前端按 `js/common.js`、`js/tabs/*.js` 和 `js/main.js` 拆分，浏览器直接加载，无构建步骤。

免费期权报价需要本机已登录的 [Theta Terminal](https://thetadata.net/docs/Articles/Getting-Started/Getting-Started.html)，默认 `http://127.0.0.1:25503`。项目已安装全局 Temurin Java 21 和 Terminal v3；从 ThetaData 用户门户创建 API key 后，复制 `scripts/theta.env.example` 为 `runtime/theta/.env` 并填入密钥，再运行 `sh scripts/start-theta.sh`。启动器会优先使用全局 Java 21。可用 `THETA_URL` 覆盖。Theta 免费层历史窗口滚动，越早回填越能保留完整 2026 年报价。BOXX、SPY、QQQM、汇率与短期利率的默认适配器使用 Yahoo Finance；VIX 使用 Cboe 官方历史 CSV。各条记录注明来源。历史 Delta 使用真实 bid/ask 中价反推隐含波动率，再通过含股息 CRR 美式期权树计算；不是交易所提供的真实 Greeks。

备用的公开期权报价渠道见 [DATA_SOURCES.md](DATA_SOURCES.md)：MarketData.app 免费账户可查询滚动一年历史链，适合作为需要独立令牌的补齐来源；DoltHub 仅用于交叉核验。任何备用渠道缺少关键 bid/ask 时，系统不会以估算价格成交。

```bash
python3 manage.py daily                  # 补漏同步并重放
python3 manage.py benchmark --start 2024-01-01  # QQQI 基准日价与分红
python3 manage.py backtest --years 2 --end 2026-10-16  # 独立两年模型回测
python3 manage.py replay                 # 从原始行情全量确定性重放
python3 manage.py status                 # 最新完整日与数据缺口
python3 -m pytest -q
```

如另有可匹配的 DoltHub 链 CSV（`date,expiration,strike,right,delta`），可运行 `python3 scripts/check-delta.py sample.csv` 抽查模型 Delta 偏差。该数据只用于交叉核验，不作为 2026 年主报价。

收益概览以 QQQI 为总回报基准，比较策略总财富（净值加已提取金额）与 QQQI 价格及分红再投资表现，并可查看双方回撤走势。基准缺失时不展示估算比较结果。没有 Theta 期权报价时，收益概览仍展示已归档的真实股票、VIX 和汇率日价及 2026 年市场走势，并明确标示模拟账本的截止日。缺失的期权数据不会用估算成交价替代。API：`/api/overview`、`/api/allocation`、`/api/trades?limit=50&offset=0`、`/api/review`、`/api/data-health`、`/api/market`。所有响应含 `as_of` 与规则版本；`market_as_of` 表示四个市场指标都有日价的最后日期。

复盘页的“模型回测”与正式 2026 日终报价账本完全隔离。它使用真实 SPY、VIX、利率、汇率和分红输入，按 CRR 美式模型生成期权 bid/ask；模型成交记录不会写入 `tianfu.sqlite3`，也不会出现在收益概览或真实交易历史。两年和十年回测分别通过 `/api/backtests/2`、`/api/backtests/10` 读取；前者展示月度归因和提取额，十年视图将相同账本口径聚合为年度收益、年度提取与年度总财富收益率。交易记录可用 `view=events` 查看逐事件，或用 `view=orders` 将期权合约及股票持仓批次的开平仓和已实现盈亏汇总为一行。价内 PUT 到期时先卖出可用 BOXX；若仍不能覆盖全部整百股接货，系统按可核验的日终报价买回不足资金对应的 PUT 合约，只接收余下可全额支付的股票。不会引入借款或卖出已有 SPY 为新接货筹资。

macOS 定时同步运行 `sh scripts/install-launchagent.sh` 安装到 `~/Library/LaunchAgents/`。模板设本机每天 09:00 检查完整交易日，适合中国时区；其他时区请调整 `StartCalendarInterval`。任务会在启动时和每日重复运行，账本重放幂等。应用启动时可用 `sh run.sh`，后台会自动补齐遗漏交易日，页面先展示上次完整结果。建议在 Theta Terminal 已启动并登录后启用定时任务。

## GitHub Pages 概览

GitHub Pages 没有本机 Flask 与 SQLite 服务，因此项目根地址会自动跳转到 `#overview`，并使用 `assets/overview-snapshot.json` 展示与本地“收益概览”相同的页面结构、图表和已归档数据。每次需要发布最新概览前，先在本机完成行情同步与重放，再执行：

```bash
python3 scripts/export-pages-snapshot.py
```

随后提交并推送 `assets/overview-snapshot.json`。本地 `localhost:10020` 始终优先读取实时 API，不使用该快照。

本机项目位于 `Documents` 时，macOS 可能拒绝 launchd 后台进程读取项目文件。安装器会报告首次退出码，错误日志位于 `data/launch.err.log`。出现 `Operation not permitted` 时，需要在系统“隐私与安全性 → 文件与文件夹 / 完全磁盘访问权限”为执行任务的 shell 授权，或将项目运行副本放在非受保护目录后重新安装。

## 模型边界

- 只模拟日终报价。开空仓按 bid，买回按 ask，长保险买入按 ask、卖出按 bid；每张合约佣金默认 0.65 美元。成交、Delta、结算均为模型假设，不能代表真实可成交价格。
- 不推断日内触发或提前指派。价内 PUT 到期接货，Covered Call 到期实物交割；价外 PUT 在到期前一周结清。
- 保险长 PUT 在到期前一周按 bid 卖出，以避免用仅有日价去假定美式期权的实物行权。已有未平仓保险时不重复购买，信号日会记录为跳过。
- 只有完整数据日才发布净值；缺报价、倒挂或无法求根时记录缺口并暂停相关交易。
- 四周阶梯的 25% 是调度单元，不是单张 PUT 的行权额上限；未接货的常规收租单元固定卖出 3 张目标 Delta PUT，不再以 BOXX/现金全额备兑余额筛选或降为 2、1 张。接货并持有 SPY 后，每周仅对已有整百股 SPY 卖数量相同的 Covered Call，并持有至到期；同时常规 PUT 恢复为单张，且行权额不得超过扣减现有短 PUT 后的 BOXX 加现金剩余额度。PUT 仅在模型绝对 Delta 距 0.16（VIX≤22）或 0.12（VIX>22）不超过 0.03 时开仓；Covered Call 固定目标为 0.25，使用相同容差。VIX<15 的 21% 上限仅作为账本审计字段记录。
- 每月按可分配已实现利润尽可能支付人民币目标；不足部分以人民币欠额累积到下一月，已占用的期权备兑额不会用于分配。
- 每月目标人民币 20,000，仅在累计已实现净利润足以支付当月目标和此前欠额时全额提取，否则全额递延。

项目不接交易账户，也不会下真实订单。SQLite 原始报价与衍生账本位于 `data/tianfu.sqlite3`，该目录默认不纳入 Git。
