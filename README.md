# Crypto Arbitrage Hub

本地套利机会监控网站。当前版本提供模拟行情、套利机会计算、策略管理界面，并预留 MySQL 接入。

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

> 此项目仅用于研究与监控。真实下单前请完成密钥加密、风控、限额、滑点与资金费率核验。
