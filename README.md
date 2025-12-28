# 📚 搜书吧助手

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

本插件是为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 项目开发的，旨在帮助用户快速获取搜书吧等网站的最新可用链接，并支持搜索。

## ✨ 特性

* **多站支持**：支持搜书吧、尚香书苑、第一会所、第一版主、有爱爱等多个站点。
* **智能导航**：自动遍历备用域名，找到可访问的最新网址。
* **搜索功能**：支持搜书吧和尚香书苑搜索。
* **灵活配置**：支持自定义搜索结果数量和站点凭据。

## 📖 使用方法

插件成功加载后，你可以使用以下命令：

### 1. 搜书吧

* **`/ssb`** 或 **`/搜书吧`**：获取搜书吧最新可用网址。
* **`/ssb [关键词]`**：在搜书吧内搜索书籍。
  * *注意：需在配置中填写 `ssb_auth`。*

### 2. 尚香书苑

* **`/sxsy`** 或 **`/尚香书苑`**：获取尚香书苑最新可用网址。
* **`/sxsy [关键词]`**：在尚香书苑内搜索书籍。
  * *注意：需在配置中填写 `sxsy_cookie`。*

### 3. 其他站点

* **`/sis`** 或 **`/第一会所`**：获取第一会所最新网址。
* **`/01bz`** 或 **`/第一版主`**：获取第一版主最新网址。
* **`/uaa`** 或 **`/有爱爱`**：获取有爱爱最新网址。

## ⚙️ 配置说明

在 AstrBot 管理面板的插件配置中，你可以设置以下项：


| 配置项                | 说明                           | 格式/示例          |
| :-------------------- | :----------------------------- | :----------------- |
| `ssb_auth`            | 搜书吧账号和密码，用于登录搜索 | `账号&密码`        |
| `sxsy_cookie`         | 尚香书苑的浏览器 Cookie        | `__cf_bm=xxx; ...` |
| `search_result_count` | 搜索结果返回的数量 (5-20)      | `10` (默认)        |

## 📝 版本历史

### v1.1

* 新增 **尚香书苑**、**第一会所**、**第一版主**、**有爱爱** 的网址获取。
* 新增 **搜书吧** 和 **尚香书苑** 的搜索功能。
* 支持配置搜索结果数量。

### v1.0

* 初始版本。
* 支持获取搜书吧 (`/ssb`) 的最新网址。

## ❤️ 支持

* [AstrBot 帮助文档](https://astrbot.app)
* 如果您在使用中遇到问题，欢迎在 [GitHub Repo](https://github.com/Foolllll-J/astrbot_plugin_soushuba) 提交 [Issue](https://github.com/Foolllll-J/astrbot_plugin_soushuba/issues)。

---
