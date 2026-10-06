# Godlike 自动续期脚本（登录版 + 外链版）

本仓库提供两个 **Godlike 免费 Minecraft 服务器** 自动续期脚本，均基于 GitHub Actions，可按需选择或同时启用。

- **登录续期版** (`main.py` / `Godlike_Renew.yml`)  
  使用邮箱/密码登录面板，通过 API 直接模拟视频观看完成续期，并通过 WebSocket 自动开机。稳定性高、无需破解验证码。

- **外链续期版** (`renew.py` / `Godlike_URL_Renew.yml`)  
  使用服务器的公开续期链接，自动处理 reCAPTCHA 音频验证，支持 IP 封锁自动更换 WARP。无需账号密码，一个链接即可续期。

---

## 🔍 哪个版本适合你？

| 需求 | 推荐使用 |
|------|---------|
| 有 Godlike 账号密码，希望一套配置管所有服务器 | 登录续期版 |
| 不想暴露账号密码，只想续期个别服务器 | 外链续期版 |
| 需要续期后自动开机 | 登录续期版（内置 WebSocket 开机） |
| 账号开启了二次验证或登录页面复杂 | 外链续期版（绕过登录） |
| 追求高稳定性、低失败率 | 登录续期版（API 直调，无验证码） |

> 📌 两个工作流完全独立，可以 **同时启用**，互不影响。建议登录版作为主力，外链版作为备用。

---

## ✨ 功能对比

| 功能 | 登录续期版 | 外链续期版 |
|------|:---:|:---:|
| 多账号/多服务器 | ✅（最多5个账号） | ✅（任意数量 ID） |
| 续期方式 | API 直调（模拟视频观看） | 浏览器操作 + reCAPTCHA 破解 |
| 续期后自动开机 | ✅（WebSocket） | ❌（依赖面板勾选） |
| 代理支持 | ✅（VLESS/VMess/Trojan等） | 内置 WARP 切换 |
| 24h上限/冷却期识别 | ✅ | ✅ |
| Telegram 通知 | ✅ | ✅（带截图） |
| 手动触发时可指定部分对象 | ❌ | ✅ |
| 登录重试 | ✅（最多3次） | ❌ |
| HTTP 连接池复用 | ✅（requests.Session） | ✅（requests.Session） |
| 依赖缓存 | ✅（Playwright 缓存） | ✅（pip 缓存） |

---

## 📂 项目结构

```
Godlike-main/
├── .github/workflows/
│   ├── Godlike_Renew.yml        # 登录版工作流
│   └── Godlike_URL_Renew.yml    # 外链版工作流
├── img/
│   └── Godlike.png              # 配置示意图
├── main.py                      # 登录版脚本
├── renew.py                     # 外链版脚本
├── requirements.txt             # Python 依赖
├── .gitignore
└── README.md
```

---

# 一、登录续期版 · 配置与使用

> 注册地址：[https://godlike.host/cart-free](https://godlike.host/cart-free)  
> ⚠️ 忘记密码？在此重置：[https://panel.godlike.host/auth/password](https://panel.godlike.host/auth/password)

自动登录 Godlike 面板，通过 API 模拟视频观看完成续期，并通过 WebSocket 自动开机。支持多账号、多种代理协议、登录重试，并通过 Telegram 发送通知。

## 1.1 配置 Secrets

在仓库 `Settings` → `Secrets and variables` → `Actions` 中添加：

| Secret 名称 | 必填 | 说明 | 示例 |
|------------|:---:|------|------|
| `GODLIKE_1` | ✅ | 主账号，格式 `邮箱-----密码` | `admin@example.com-----MyPassword` |
| `GODLIKE_2` ~ `GODLIKE_5` | ❌ | 额外账号（最多到5） | 同上 |
| `TG_BOT_TOKEN` | ❌ | Telegram Bot Token | `1234567890:AAE...` |
| `TG_CHAT_ID` | ❌ | 接收通知的 Chat ID | `123456789` |
| `PROXY_NODE` | ❌ | 代理节点链接（可选） | 见代理格式说明 |

**代理节点格式（可选）：**

| 协议 | 示例 |
|------|------|
| VLESS | `vless://uuid@host:port?type=ws&security=tls&sni=...` |
| VMess | `vmess://eyJhZGQiOi...` |
| Trojan | `trojan://password@host:port?type=ws&sni=...` |
| Shadowsocks | `ss://YWVzLTI1Ni1nY206...` |
| SOCKS5 | `socks5://user:pass@host:port` |

## 1.2 使用方法

- **定时运行**：默认每 3 小时执行一次（UTC），可在 `.github/workflows/Godlike_Renew.yml` 中修改 `cron`。
- **手动触发**：`Actions` → `Godlike 续期` → `Run workflow`。
- **API 调用**：
  ```bash
  curl -X POST \
    -H "Authorization: Bearer ghp_xxxx" \
    -H "Accept: application/vnd.github.v3+json" \
    https://api.github.com/repos/你的用户名/仓库名/actions/workflows/Godlike_Renew.yml/dispatches \
    -d '{"ref":"main"}'
  ```

# 二、外链续期版 · 配置与使用

> 注册地址：[https://godlike.cool](https://godlike.cool)  
> 管理面板：[https://panel.godlike.host](https://panel.godlike.host)

基于公开续期页面，自动处理 reCAPTCHA 音频验证、IP 封锁自动切换 WARP。一个 ID 对应一台服务器，无需提供账号密码。

## 2.1 为服务器开启外链续期并获取 ID

1. 登录 [Godlike 面板](https://panel.godlike.host)，点击要续期的服务器。
2. 左侧菜单 → **Public Renewal**。
3. 在 **URL** 输入框中填入自定义标识（**强烈建议直接使用服务器 ID**，如地址栏中的 `8xxxabcd`）。
4. 勾选 **Start server on renewal**（续期后自动开机）。
5. 点击 **Save Renewal Page**。

保存后，续期链接为 `https://godlike.cool/你填的ID`。  
**将此 ID 填入 GitHub Secrets 的 `GODLIKE_ID` 字段**。

![外链配置](./img/Godlike.png)

## 2.2 配置 Secrets

| Secret 名称 | 必填 | 说明 | 示例 |
|------------|:---:|------|------|
| `GODLIKE_ID` | ✅ | 上一步设置的 ID，多个用换行分隔 | `8xxxabcd`<br>`xyz789` |
| `TG_BOT_TOKEN` | ❌ | Telegram Bot Token | `123456:ABC-...` |
| `TG_CHAT_ID` | ❌ | 接收消息的 Chat ID | `123456789` |

> 如果不配置 Telegram 相关 Secret，脚本仍会正常续期，仅不发通知。

## 2.3 使用方法

- **定时运行**：默认每 90 分钟执行一次（UTC），可在 `Godlike_URL_Renew.yml` 工作流文件中调整。
- **手动触发**：`Actions` → `Godlike URL 续期` → `Run workflow`，可以在 **指定ID** 输入框中填写要续期的 ID（逗号或换行分隔），留空则处理全部 `GODLIKE_ID`。
- **API 调用**：
  ```bash
  curl -X POST \
    -H "Authorization: Bearer ghp_xxxx" \
    -H "Accept: application/vnd.github.v3+json" \
    https://api.github.com/repos/你的用户名/仓库名/actions/workflows/Godlike_URL_Renew.yml/dispatches \
    -d '{"ref":"main", "inputs": {"accounts": "8xxxabcd"}}'
  ```

---

# 三、Telegram 通知配置（通用）

两个脚本共用以下 Secrets 来发送 Telegram 通知：

- `TG_BOT_TOKEN`：向 [@BotFather](https://t.me/BotFather) 发送 `/newbot` 创建机器人获取。
- `TG_CHAT_ID`：向 [@userinfobot](https://t.me/userinfobot) 发送任意消息获取。

配置后，续期成功、24h 上限、冷却期、失败都会推送图文消息。

---

# 🐛 常见问题（整合）

## 登录版常见问题

**1. 登录失败**
- 检查 `GODLIKE_X` 格式（邮箱-----密码）。
- 脚本会自动重试 3 次，如果仍然失败，检查账号密码是否正确。
- 如"Through login/password"按钮未出现，页面可能已更新，请提交 Issue。

**2. 代理连接失败**
- 确认 `PROXY_NODE` 格式正确。
- 检查代理节点是否可用。
- 如不需要代理，删除 `PROXY_NODE` Secret 即可。

## 外链版常见问题

**3. 如何获取服务器 ID？**
- 面板地址栏中的 `https://panel.godlike.host/server/8459d9bd`，`8459d9bd` 为服务器 ID。  
- 但在 `GODLIKE_ID` 中填写的是你在 **Public Renewal 页面自己设定的 URL 标识**，建议与服务器 ID 保持一致。

**4. reCAPTCHA 识别失败**
- 音频识别会重试最多 3 次；若 IP 被 Google 标记，脚本会通过 WARP 自动换 IP，每个 ID 最多尝试 20 次。

**5. "24小时上限"或"冷却期"是什么意思？**
- 24h 上限：服务器已攒满 24 小时运行时间，无法再增加，脚本视为成功并跳过。
- 6 分钟冷却期：同一服务器续期后 6 分钟内无法再次续期，脚本会跳过，等待下次调度。

**6. 运行时间很长是否正常？**
- 正常，单个 ID 约 2-5 分钟，若触发 IP 切换会更久，GitHub Actions 最长允许 6 小时。

## 通用问题

**7. 截图在哪里查看？**
- 每次 Actions 运行结束，底部的 **Artifacts** 区域可下载截图包。

**8. 没有收到 Telegram 通知？**
- 确认 `TG_BOT_TOKEN` 和 `TG_CHAT_ID` 正确。
- 先在 Telegram 给 Bot 发送 `/start` 激活会话。

---

# 🔒 安全建议

- 所有敏感信息均存储在 GitHub Secrets 中，Actions 日志已脱敏（邮箱和服务器ID在日志中部分隐藏）。
- TG 通知中使用真实邮箱和服务器 ID，仅你可见。
- 外链版无需提供账号密码，更安全。
- 定期更新 Fork 仓库以获取最新修复。

---

# 📄 许可证

MIT License

---

**⚠️ 免责声明**：本脚本仅供学习交流使用，使用者需遵守 Godlike 的服务条款。因使用本脚本造成的任何问题，作者不承担任何责任。
