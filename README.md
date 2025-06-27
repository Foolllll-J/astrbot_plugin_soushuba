## 💡 简介

本插件是为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 项目开发的，旨在帮助用户**快速获取搜书吧的最新可用链接**。它能自动遍历备用域名，告别链接失效的烦恼，一键直达搜书资源。

## ✨ 特性

* **智能重试**：自动遍历预设的搜书吧域名列表，找到第一个可访问的网站。
* **链接提取**：从成功访问的网页中智能提取目标链接。
* **简单易用**：通过简单的命令即可操作。

## 📝 使用方法

插件成功加载后，你可以在 QQ 群或私聊中使用以下命令：

* **`/ssb`**：依次尝试访问预设的搜书网站列表，并返回第一个成功访问到的页面中的第一个链接。
  **用法**：
  ```
  /ssb
  ```

## ⚙️ 配置说明

插件的网址列表在源代码中硬编码于 `SoushuBaLinkExtractorPlugin` 类的 `self.target_domains` 变量中，你可以根据需要自行修改。

## 📄 许可证

本项目基于 [MIT 许可证](https://www.google.com/search?q=LICENSE) 发布。

## 📞 联系方式

GitHub Issues: [https://github.com/Foolllll-J/astrbot\_plugin\_soushuba/issues](https://www.google.com/search?q=https://github.com/Foolllll-J/astrbot_plugin_soushuba/issues)

---
