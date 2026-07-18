# CoinGlass 爆仓数据接入记录

更新时间：2026-07-18

## 页面入口

- 全市场爆仓页：`https://www.coinglass.com/zh/liquidations/`
- 单币爆仓页：`https://www.coinglass.com/zh/liquidations/{SYMBOL}`
- 示例：`https://www.coinglass.com/zh/liquidations/AKE`

## 当前验证结果

CoinGlass 单币页面渲染后可以读到以下数据：

- 当日总爆仓
- 多单爆仓
- 空单爆仓
- 市场爆仓状态
- 爆仓人数
- 最大单笔爆仓
- 爆仓高峰时段
- 爆仓量相对 7 日均值、30 日峰值
- 交易所爆仓分布
- 1H / 4H / 12H / 24H 爆仓汇总
- 实时爆仓列表

以 `AKE` 页面测试时，页面可见数据包含交易所分布（Bybit、Binance、Gate 等）、1H/4H/12H/24H 多空爆仓汇总，以及实时爆仓明细。

## 接入方案优先级

1. 官方 CoinGlass API
   - 最稳定、最适合批量扫描和历史回补。
   - 当前测试的 API 权限返回 `Upgrade plan`，说明需要确认套餐权限后才能正式接入。

2. 页面渲染解析
   - 可以用浏览器/自动化方式打开单币爆仓页，读取渲染后的 DOM 数据，再写入 MySQL。
   - 适合 AKE、US、T 这类重点盯盘币的低频补充数据。
   - 不适合全市场高频扫描，否则资源占用会偏高，也容易被网页风控影响。

3. 内部接口裸爬
   - 前端脚本里存在 `/api/futures/liquidation/today`、`/api/coin/liquidation` 等内部接口。
   - 直接裸请求会出现 `success=true` 但没有 `data` 的情况，说明还存在前端请求封装、参数或响应处理。
   - 不建议把这种半可用接口直接接进系统，避免“缺数据”被误判为“爆仓为 0”。

## 策略使用规则

- 爆仓数据不单独决定看多或看空，只用于解释结构变化。
- 当持仓、CVD、多空人数比突然变化时，需要结合爆仓数据判断：
  - 是主动开仓/平仓导致的变化；
  - 还是多头或空头被强平导致结构线断裂。
- 犄角走势延续判断中，如果持仓或多空人数比突然跌破结构线，需要优先检查是否有集中爆仓；没有爆仓支撑时，不能继续给高分。

## 建议落库表

建议后续单独建表，不和现有行情快照混在一起：

- `coinglass_liquidation_summary`
  - `symbol`
  - `window`：1H / 4H / 12H / 24H / today
  - `long_liquidation_usd`
  - `short_liquidation_usd`
  - `total_liquidation_usd`
  - `long_ratio`
  - `short_ratio`
  - `source`
  - `snapshot_at`

- `coinglass_liquidation_exchange`
  - `symbol`
  - `exchange`
  - `long_liquidation_usd`
  - `short_liquidation_usd`
  - `total_liquidation_usd`
  - `share_ratio`
  - `snapshot_at`

- `coinglass_liquidation_orders`
  - `symbol`
  - `exchange`
  - `price`
  - `amount_usd`
  - `side`
  - `liquidated_at`
  - `snapshot_at`
