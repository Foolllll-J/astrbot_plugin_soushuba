<div align="center">

# 📚 搜书吧助手

<i>🧭 搜书吧导航，助你永不失联</i>

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

</div>

## ✨ 简介

一款为 [**AstrBot**](https://github.com/AstrBotDevs/AstrBot) 设计的搜书吧适配插件，旨在帮助用户快速获取搜书吧等网站的最新可用链接，并支持搜索与监控论坛可用性。

---

## 🚀 功能特性

* **多站支持**：搜书吧、尚香书苑、第一会所、第一版主、有爱爱。
* **智能导航**：自动从导航入口解析可用真实网址。
* **站内搜索**：支持搜书吧与尚香书苑关键词搜索。
* **状态监控**：支持按会话订阅监控通知，成功不通知，失败告警，恢复通知。

---

## 📖 使用方法

插件加载后可使用以下命令：

### 1. 搜书吧

* `/ssb` 或 `/搜书吧`：获取搜书吧最新地址。
* `/ssb <关键词>`：在搜书吧内搜索书籍。

说明：`/ssb <关键词>` 需要配置 `ssb_auth`（格式：`账号&密码`）。

### 2. 尚香书苑

* `/sxsy` 或 `/尚香书苑`：获取尚香书苑最新地址。
* `/sxsy <关键词>`：在尚香书苑内搜索书籍。

说明：`/sxsy <关键词>` 需要配置 `sxsy_cookie`。

### 3. 其他站点

* `/sis` 或 `/第一会所`：获取第一会所最新地址。
* `/01bz` 或 `/第一版主`：获取第一版主最新地址。
* `/uaa` 或 `/有爱爱`：获取有爱爱最新地址。

### 4. 网站状态监控（管理员）

* `/ssbmon` 或 `/监控搜书吧`：在当前会话订阅搜书吧状态通知。
* `/ssbmon off`：在当前会话取消搜书吧状态订阅。
* `/sxsymon` 或 `/监控尚香书苑`：在当前会话订阅尚香书苑状态通知。
* `/sxsymon off`：在当前会话取消尚香书苑状态订阅。


---

## ⚙️ 配置说明

在管理面板的插件配置中可设置：

| 配置项 | 说明 | 格式/示例 |
| :-- | :-- | :-- |
| `ssb_auth` | 搜书吧账号和密码，用于登录搜索 | `账号&密码` |
| `sxsy_cookie` | 尚香书苑浏览器 Cookie | `__cf_bm=xxx; ...` |
| `search_result_count` | 每次搜索返回的结果数（5-20） | `10` |
| `monitor_check_interval` | 监控检测间隔（秒），默认 3600，最小 10 | `3600` |

---

## 📘 更新日志

详见 [CHANGELOG.md](./CHANGELOG.md)

---

## ❓ 支持

* [AstrBot 帮助文档](https://astrbot.app)
* 如果您在使用中遇到问题，欢迎在本仓库提交 [Issue](https://github.com/Foolllll-J/astrbot_plugin_soushuba/issues)。

---

<div align="center">

**如果本插件对你有帮助，欢迎点个 ⭐ Star 支持一下！**

</div>
