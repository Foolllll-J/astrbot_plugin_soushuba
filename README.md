<div align="center">

# 📚 搜书吧助手

<i>🧭 搜书吧导航，助你永不失联</i>

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

</div>

## ✨ 简介

一款为 [**AstrBot**](https://github.com/AstrBotDevs/AstrBot) 设计的搜书吧适配插件，旨在帮助用户快速获取搜书吧等网站的最新可用链接，并支持搜索下载与监控论坛可用性。

---

## 🚀 功能特性

* **多站支持**：搜书吧、SXSY、第一会所、第一版主、有爱爱。
* **智能导航**：自动从导航入口解析可用真实网址。
* **站内搜索**：支持搜书吧与SXSY关键词搜索。
* **直接下载**：支持将帖子序号作为参数直接发起附件下载。
* **额度控制**：支持按日限制单用户/全局可用币额度（管理员不受限）。
* **状态监控**：支持按会话订阅监控通知，成功不通知，失败告警，恢复通知。

---

## 📖 使用方法

插件加载后可使用以下命令：

### 1. 搜书吧

* `/ssb` 或 `/搜书吧`：获取搜书吧最新地址。
* `/ssb <关键词>`：在搜书吧内搜索书籍。
* `/ssb <序号>`：从上一次搜索结果中选择对应帖子并下载附件。
* `/ssb <帖子URL>`：直接解析并下载该帖子附件（附带能力）。

说明：`/ssb <关键词>` 需要配置 `ssb_auth`（格式：`账号&密码`）。

### 2. SXSY

* 用法同 `/ssb`

说明：`/sxsy <关键词>` 需要配置 `sxsy_url` 与 `sxsy_cookie`（用于搜索和状态监控的站点地址）。

### 3. 其他站点

* `/sis` 或 `/第一会所`：获取第一会所最新地址。
* `/01bz` 或 `/第一版主`：获取第一版主最新地址。
* `/uaa` 或 `/有爱爱`：获取有爱爱最新地址。

### 4. 网站状态监控（管理员）

* `/ssbmon` 或 `/监控搜书吧`：在当前会话订阅搜书吧状态通知。
* `/ssbmon off`：在当前会话取消搜书吧状态订阅。
* `/sxsymon` 或 `/监控SXSY`：在当前会话订阅SXSY状态通知。
* `/sxsymon off`：在当前会话取消SXSY状态订阅。

---

## ⚙️ 配置说明

首次加载后，请在 AstrBot 后台 -> 插件 页面找到本插件进行设置。所有配置项都有详细的说明和提示。

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
