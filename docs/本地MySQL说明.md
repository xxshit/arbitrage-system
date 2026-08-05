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

本地与云端备份链路已经接通：

- 云端每 6 小时生成一致性压缩备份；
- 本机可通过 `scripts/pull_cloud_mysql_backup.ps1` 下载并校验云端 SQL 与匹配聊天密钥；
- `scripts/sync_cloud_to_local.ps1` 会先生成本地回退库，再把云端快照恢复到本机；
- `scripts/start_workstation.ps1` 用于换电脑开始工作时先同步 Git，再同步本地数据库副本。

2026-08-05 已完成一次真实恢复验证：本地恢复后为 36 张表、2 个账号、238 条协作记录，最新行情快照为 2026-08-05 09:31:19（数据库存储时间），聊天密钥哈希与该次云端备份附带密钥一致。上述数值只用于记录本次验证，不是长期固定值；后续以 MySQL 实际状态为准。
