# Onebot Maa Log Analyzer

AstrBot 的 AIOCQHTTP 插件，用于分析 Maa 日志包、引用追问和执行 `/maa` 管理指令。

## 安装与配置

- 需要 AstrBot `>=4.29.0,<5` 和 `aiocqhttp` 平台适配器。
- 将插件包解压到 AstrBot 的 `data/plugins/`，然后在插件管理中启用。
- 首次启动会在 `data/plugin_data/OnebotMaaLogAnalyzer/` 创建默认 `config.json`。
- 在插件面板配置监听群和 LLM 等选项；`config_file` 留空时使用默认配置文件，填写时应指定文件路径。

详细说明参见[部署文档](https://github.com/Hollow-YK/OnebotMaaLogAnalyzer/blob/main/docs/astrbot.md)和[配置文档](https://github.com/Hollow-YK/OnebotMaaLogAnalyzer/blob/main/docs/configuration.md)。真实 AstrBot 与 NapCat / Lagrange 的端到端联调仍需在部署环境中验证。
