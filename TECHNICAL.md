# 技术说明

## 环境与启动

推荐 Python 3.11；本项目测试也在本机 Python 3.9.6 运行。安装 `requirements.txt` 后可用 `python3 run.py`、`sh run.sh` 或双击 `启动天府退休计划.command`。默认绑定 `127.0.0.1:10020`，可用 `PORT` 覆盖；1024 以下端口在 macOS 需要管理员权限。数据库路径默认 `data/tianfu.sqlite3`，测试和隔离运行可用 `TIANFU_DB` 覆盖。

前端直接加载 ES modules，无 npm、打包步骤或 CDN 运行依赖。模块入口为 `js/main.js`，`js/app.js` 仅作兼容入口。

`runtime/` 是本机 Terminal 与本地配置目录，已由 `.gitignore` 忽略。`sh scripts/start-theta.sh` 优先使用系统安装的 Java 21，并从 `runtime/theta/.env` 读取 `THETADATA_API_KEY` 后启动本机 Terminal；密钥不写入项目源码或账本。

## 行情与期权口径

- 回填先读取 2025 全年 SPY 分红等模型预热数据；模拟自 2026-01-01 建账。期权只从 2026 年开始归档。
- Theta Terminal 的 `/v3/option/history/eod` 返回 EOD 报告的最后 NBBO。合约键由 `SPY:到期日:方向:行权价` 组成，不能单独使用响应中的 `symbol`。
- `option_quotes` 保存 bid、ask、来源与报告时间。倒挂、非正 ask、无效规模或报告日不符时不写入。开空按 bid，买回按 ask；长保险反向。不会把收盘成交价当作可成交报价。
- 合约筛选先用欧式模型快速预排候选，再对前 30 个用含股息 CRR 美式模型反推 IV 和 Delta；成交事件记录模型 Delta。这个预排是性能优化，模型 Delta 不是数据商 Greeks。
- 缺持仓盯市报价或交易必需的 Delta 时停止发布当日结果并记录缺口。缺失的一日会阻止后续日期越过它。

## 策略关键约束

四周阶梯每周轮到一个 25% 净值调度单元，该比例不作为单张 PUT 的行权额上限。未接货时，每个常规单元固定卖出 3 张目标 Delta PUT；不卖常规 CALL，也不以 BOXX/现金全额备兑余额筛选或降为 2、1 张。PUT 只接受模型绝对 Delta 距 VIX 对应目标（0.16 或 0.12）不超过 0.03 的合约。接货并持有 SPY 后，房东模式以已有整百股卖出数量相同的 Covered Call，固定目标 Delta 为 0.25、行权价不得低于解套成本，并持有至到期实物交割；同时常规 PUT 恢复为一张，并受扣减已有短 PUT 后的 BOXX 加现金剩余额度限制。低 VIX 的 21% 上限仅作为账本审计字段记录。已有未平保险 PUT 时不会重复购买。月末将全部可分配已实现利润支付为人民币，未支付部分累积为下月欠额。

保险触发取上一交易日 VIX 和 QQQM MA20；年度累计保险买入费不超过年度累计已实现净利润的 1.6%。月末人民币 20,000 提取只能由可分配已实现净利润覆盖，欠额全额递延。三张未备兑筛选的 PUT 同时接货且 BOXX 已耗尽时，按当日 SPY 收盘价清算未被 Covered Call 占用的 SPY 股数补足接货资金；事件标记为 `LIQUIDATE_SPY`，若仍不足才记为策略违约。

## 重放与检查

```bash
python3 manage.py backfill --start 2025-01-01
python3 manage.py daily
python3 manage.py replay
python3 manage.py status
python3 -m pytest -q
node --check js/main.js
```

`tests/test_simulation.py` 覆盖资金上限、阈值、接货交割、保险、跨月欠额、价格模型、来源解析、幂等、净值恒等式及 API。第三方数据未接通前，这些测试使用固定夹具而非虚构正式业绩。
