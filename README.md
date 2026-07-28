# Crypto Arbitrage Hub

本地套利机会监控网站。当前版本使用交易所公开 API、MySQL 最新快照、趋势扫描、报警、账号权限与运行规则管理。

## 启动

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
py app.py
```

打开 http://127.0.0.1:5000 。也可双击 `启动网站.vbs` 静默启动：它不会弹出命令提示符窗口。

## 使用 MySQL

1. 创建数据库：`CREATE DATABASE arbitrage_hub CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;`
2. 复制 `.env.example` 为 `.env`，填入数据库连接。
3. 重启应用；数据表会自动创建。

## 创建或重置管理员

首次部署到一台全新的数据库时执行：

```powershell
.\.venv\Scripts\flask.exe --app app create-admin --username owner
```

命令会安全地提示输入密码，数据库只保存密码哈希。登录后可在左侧“运行规则”生成一次性邀请码，普通账号使用邀请码注册。同一账号的新登录会让旧设备自动退出。

> 此项目仅用于研究与监控。真实下单前请完成密钥加密、风控、限额、滑点与资金费率核验。
