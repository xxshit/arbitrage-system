# 新电脑恢复与 Codex 交接

## 三类内容分别如何恢复

| 内容 | 备份位置 | 恢复方式 |
| --- | --- | --- |
| 代码、页面、规则文档、项目大脑 | GitHub 仓库 | 在新电脑克隆仓库 |
| 行情快照、报警、账号、邀请码、盯盘状态等运行数据 | 云端 MySQL 与数据库备份 | 继续连接现有云端网站，或从备份恢复数据库 |
| Codex 对话任务 | Codex 账号中的任务记录 | 使用同一账号登录 Codex，打开原任务；是否完整出现以账号同步状态为准 |

GitHub 不包含 `.env`、数据库密码、SSH 私钥、Webhook 和 MySQL 数据，这是必要的安全边界。因此只有 GitHub 仍不足以完整重建正在运行的系统，数据库备份和安全配置也必须保留。

## 新电脑继续使用现有云端网站

普通使用者只需要浏览器和网站账号，直接打开云端 HTTPS 地址，不需要安装 Python、Git 或 MySQL。

## 新电脑继续开发项目

1. 安装 Git、Python 3.11+、Codex 桌面应用；需要本地数据库时再安装 MySQL 8。
2. 使用同一个 GitHub 账号配置 SSH 或 HTTPS 凭据。
3. 克隆仓库：

   ```powershell
   git clone git@github.com:xxshit/arbitrage-system.git
   cd arbitrage-system
   ```

4. 创建虚拟环境并安装依赖：

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

5. 从安全的线下位置恢复 `.env`、SSH 私钥或数据库凭据；不要从 GitHub 获取或上传这些内容。
6. 使用同一 OpenAI/Codex 账号登录，在 Codex 中打开该仓库。
7. 如果原任务可以在任务列表中看到，直接打开原任务继续；如果看不到，新建任务并先让 Codex 阅读根目录 `AGENTS.md` 和 `docs/README.md`。

## 格式化前检查

- Git 工作区没有未提交、未推送的修改。
- 最近一份 MySQL 云端备份和异地备份均可校验、可解压。
- `.env`、SSH 私钥和必要的恢复凭据已放在安全的密码管理器或加密离线介质中。
- 记录当前云服务器、域名和 GitHub 仓库的归属账号，但不要把密码写入文档。
