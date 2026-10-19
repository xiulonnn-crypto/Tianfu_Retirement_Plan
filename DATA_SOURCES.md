# 数据源核验（2026-10-19）

本项目只用逐日真实 bid/ask 为期权模拟成交。缺一日选约或持仓盯市需要的报价时，净值停在上一个完整日。原始日价和期权报价不得混同。

## 独立历史模型回测

两年模型回测保存于 `data/backtest-2y.sqlite3`，与 `data/tianfu.sqlite3` 的真实 Theta EOD 账本隔离。它使用 Yahoo Finance 的 SPY、BIL、BOXX、QQQ、QQQI、USD/CNY、短期利率及 Cboe VIX 历史数据。期权 bid/ask 由 CRR 美式模型按当日 VIX、利率和 SPY 股息收益率计算，围绕理论中价设置固定 2% 半价差；所有模型事件均携带模型版本、波动率、利率及理论中价，不能解释为历史期权成交。

为保证历史连续性，抵押资产在 2022-12-28 前使用 BIL 总回报代理，之后使用 BOXX 总回报腿；比较基准在 2024-01-29 前使用 QQQ 总回报代理，之后使用 QQQI 实际总回报腿。代理切换和输入指纹随回测结果一同返回。

## 已接入

- BOXX、SPY、QQQM、USD/CNY、短期利率：Yahoo Finance 日数据。2025-01-02 至 2026-10-16 已入本机 SQLite。
- VIX：Cboe 官方 `VIX_History.csv`。2025-01-02 至 2026-10-16 已入本机 SQLite。
- SPY 期权：本机 Theta Terminal v3 的免费 EOD NBBO。2026-01-05 至 2026-10-16 共 198 个交易日、1,647,845 条 bid/ask 已归档；每条成交事件可关联其原始报价、报告时间和模型 Delta。
- 页面 `/api/market` 独立展示市场数据，不将其当作期权策略收益。

## 备用渠道与切换原则

- [MarketData.app 期权链](https://www.marketdata.app/docs/api/options/) 是已核验的备用来源：其历史期权链与单合约报价接口提供 bid/ask；免费账户每日 100 次请求、可查询滚动一年历史、返回至少延迟一个完整交易日的数据。若配置该渠道，应使用本机环境变量 `MARKETDATA_TOKEN` 的 Bearer 认证，令牌不得写入仓库或前端。其每日额度不足以无筛选地回填整条 SPY 链，因此适合补齐 Theta 缺失日的策略到期窗口、持仓合约与抽样核验；若关键报价仍缺失，账本继续停在上一个完整日。
- [DoltHub 公开 options 库](https://www.dolthub.com/repositories/post-no-preference/options) 无需账号，适合核验特定合约及公开数据可用性。其到期日覆盖不完整，不能替代完整回放源。
- 不以 Yahoo Finance、Cboe VIX 或模型价格填补 SPY 期权 bid/ask；这些来源分别只承担股票、指数波动率与 Delta 计算输入。

## 2026 年 SPY 期权报价核验

| 来源 | 接入与覆盖 | 核验结果 |
| --- | --- | --- |
| [DoltHub 公开 options 库](https://www.dolthub.com/repositories/post-no-preference/options) | 无账号公开 SQL API，含 2026 年 SPY 部分 bid/ask | 2026-01-05 仅有 2026-01-16、01-30、02-13、02-27 四个到期日；该日 02-06 链为 0 条。缺少策略 28–35 天窗口内的实际 SPY 周期合约，不能作完整主数据。公开 API 对 2026 全量聚合查询超时。可作抽样交叉核验。 |
| [GitHub 期权数据镜像](https://github.com/anahatsingh-ui/options-dataset-hist) | 可下载、含 bid/ask | SPY 只覆盖 2008–2025，无法回放 2026。 |
| [OptionsDX](https://www.optionsdx.com/) | 免费历史样本需注册与下载 | 核验到的免费数据未覆盖完整 2026 年。 |
| [OnclickMedia](https://www.onclickmedia.com/About) | 官网称免费数据至 2026-03-05 | 官网数据接口返回禁止自动抓取的 403；停止尝试，且截止日期早于本项目所需的 2026-10-16。 |
| [MarketData.app](https://www.marketdata.app/docs/account/free-accounts/) | 免费账号有一年历史窗口、每天 100 次请求 | 已核验为可接入备用渠道：Bearer 令牌可访问历史期权链。受免费额度和一年窗口限制，适合补齐策略所需合约，不能无筛选回填全链。 |
| [ThetaData](https://thetadata.net/docs/Articles/Getting-Started/Getting-Started.html) | 免费账号有滚动一年期权日终数据 | 已接通本机 Terminal v3 与 API key，归档完整覆盖本次 2026-01-05 至 2026-10-16 回放窗口。Terminal 返回嵌套的 `{contract, data[]}` EOD 格式，适配器会先扁平化后校验 bid/ask。 |

## 本次回放的数据质量

模拟账本完整发布至 2026-10-16。期权筛选缺口在页面以中文展示；半年期保险 PUT 的模型 Delta 反推失败或找不到匹配 Delta 时，系统记录为“跳过当日购保”，不会混同为缺失市场报价，也不会阻断短 PUT、净值或现金流回放。

### 可重复的 DoltHub 缺口查询

```sql
SELECT expiration, COUNT(*) AS n
FROM option_chain
WHERE act_symbol='SPY' AND date='2026-01-05'
GROUP BY expiration ORDER BY expiration;

SELECT COUNT(*) AS n
FROM option_chain
WHERE act_symbol='SPY' AND date='2026-01-05' AND expiration='2026-02-06';
```

公开 API：`https://www.dolthub.com/api/v1alpha1/post-no-preference/options/master?q=<URL-encoded SQL>`。第二条实测 `n=0`。不应用估算价格或放宽既定 28–35 天规则来绕过该缺口。
