# 本地 MySQL / MariaDB 说明

当前项目已经从默认 SQLite 切换到本地 MariaDB（MySQL 兼容）。

## 运行位置

- MariaDB 程序目录：`F:\mysql\mariadb-11.8.6-winx64`
- MariaDB 数据目录：`F:\mysql\data`
- Windows 服务名：`ArbitrageMariaDB`
- 服务启动方式：手动启动（按你的要求，不随 Windows 开机自动启动）

## 项目连接

项目通过 `.env` 中的 `DATABASE_URL` 连接本地数据库：

```text
mysql+pymysql://arbi:***@127.0.0.1:3306/arbitrage_hub?charset=utf8mb4
```

`.env` 不提交到 GitHub，避免泄露本地密码和 Lark webhook。

## 迁移记录

2026-07-17 已从 `instance/arbitrage_hub.db` 迁移到本地 MariaDB，共迁移约 1,256,096 行。

关键表迁移后数量：

- `futures_price_history`: 1,145,028
- `funding_rate_record`: 83,248
- `alert_event`: 6,304
- `latest_market_snapshot`: 1,215
- `latest_dual_futures_snapshot`: 1,276
- `transfer_network_snapshot`: 7,774
- `index_component_snapshot`: 1,538

## 备份原则

SQLite 原文件仍保留在：

```text
F:\套利系统\instance\arbitrage_hub.db
```

切换到 MySQL 后，新的数据会写入：

```text
F:\mysql\data
```

后续应增加自动 MySQL dump 备份，避免本地硬盘故障导致数据丢失。
