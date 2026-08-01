# 公网 HTTPS 访问

当前外部地址：

`https://arbi-k7m4p2.5-61-208-92.sslip.io:18443/`

任何电脑或手机直接使用浏览器打开即可，不需要安装项目、运行PowerShell或配置SSH密钥。

## 安全边界

- 外部浏览器只连接 Nginx 的 HTTPS 地址；Gunicorn 仍只监听`127.0.0.1:15831`。
- MariaDB 仍只监听`127.0.0.1:3306`，不存在公网数据库端口。
- 数据库应用账号只允许从`127.0.0.1`连接，并限制为本项目数据库所需的最小权限。
- 所有业务页面和接口必须先登录；普通查看账号不能修改运行规则、盯盘列表、策略或触发测试推送。
- 注册仍要求一次性邀请码；同一网站账号仍只允许一个活跃登录设备。
- 登录和注册接口在 Nginx 层限速，降低暴力尝试风险。

## 部署结构

```text
外部浏览器 → HTTPS/Nginx → 127.0.0.1:15831/Gunicorn → 127.0.0.1:3306/MariaDB
```

公网只暴露 HTTPS 反向代理，不暴露 Gunicorn 和 MySQL。证书由 Let's Encrypt 签发，PM2 中的`arbitrage-cert-renewal`每12小时检查续期。

## 运维检查

```bash
nginx -t
pm2 status arbitrage-private
pm2 status arbitrage-cert-renewal
ss -lntp | grep -E '(:3306|:15831)'
```

其中 MySQL 和 Gunicorn 必须始终显示为`127.0.0.1`监听。如果变为`0.0.0.0`或`*`，应立即停止公网访问并修复。
