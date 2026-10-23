# AI 工具

除摘要之外，AI 可用工具自主检索**日志压缩包**与**项目代码仓库**内的原始内容（只读、不落盘）。

## 日志压缩包工具

由 `log_tools_enabled` 控制（默认开启，**不依赖 repo 是否启用**）。分析路径与追问路径都会开放这些工具。

| 工具 | 用途 |
| --- | --- |
| `log_list_files` | 列出日志包内某目录的文件 |
| `log_search` | 在日志包文本文件中正则搜索（**摘要不够用时找原始证据**） |
| `log_read_file` | 读取日志包内某个文本文件（支持行范围） |
| `log_list_images` | 列出日志包内全部图片（确认能附哪些图） |

> **摘要必然有取舍。** 日志包工具让 AI 能像读仓库一样直接读压缩包内的原始文件——
> 例如摘要里被截断的完整堆栈、某个配置项的原文、某行日志的上下文。
> 压缩包**只读不落盘**（不解压到磁盘），路径同样受相对路径沙箱与敏感文件策略约束。
> 追问时也会开放这些工具，因此 AI 不再只能回答「摘要中未包含」。
> 日志路径会随 UI 不同而变化（MXU 根目录 `maafw.log`、MFAAvalonia `debug/maafw.log`、
> MaaFwApp `log/maafw.log` 与 `pi/debug/maafw.log`），工具按压缩包内实际路径访问，无需解压。

## 项目代码仓库工具

仅在 `repo.mode = "agent"` 时提供给 AI，见 [项目代码参考](repo.md)。

| 工具 | 用途 |
| --- | --- |
| `list_dir` | 列目录，了解仓库结构 |
| `search_repo` | 正则搜索，定位节点定义 / 字段 / 阈值（最高效） |
| `read_file` | 读文件，支持 `start_line` / `end_line` 分段读大文件 |
| `list_tags` | 列出可用版本 tag |
| `pull_latest` | 从当前配置的远端更新默认分支；不接受 URL 或 Git 命令 |
| `checkout_ref` | 获取 tag / commit；仅在 `allow_ai_switch_tag` 开启时提供（受 `max_tag_switch_rounds` 限制） |

## 沙箱安全

- 所有路径先 `resolve` 再校验必须位于仓库根目录内（防 `../` 与符号链接逃逸）
- 日志包内路径拒绝绝对路径与 `..` 上跳
- 拒绝读取 `.git` 内部、二进制文件与[凭据密钥类文件](attachments.md#敏感文件保护)
- 单文件读取受字节上限、单次返回受字符上限约束
- 读取仓库文件；Git 更新/切换仅通过固定工具执行，**绝不执行仓库中的任何代码或任意 Git 命令**

## 限制

agent 模式受三重限制，避免 token 与耗时失控：

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `max_tool_rounds` | `8` | 工具调用最大轮次 |
| `max_tool_calls_per_round` | `6` | 单轮最多执行的工具数 |
| `agent_deadline_seconds` | `600` | agent 循环总时长上限 |
| `max_tool_result_chars` | `30000` | 单次工具结果字符上限 |
