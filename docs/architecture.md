# 架构

## 双入口

项目保留两种部署入口，但分析链路只有一套：

```text
裸 OneBot                          AstrBot
python main.py                     astrbot_plugin/main.py
    │                                   │
    ▼                                   ▼
OneBotPlatformAdapter              AstrBotPlatformAdapter
    │                                   │
    └──────────── PlatformPort ─────────┘
                         │
                         ▼
                 MaaWorkflowRuntime
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
maa_analyzer/core  maa_analyzer/features/maa  LLM / history / render
```

- **裸 OneBot**：`main.py` 负责读取 `config.json`、建立 OneBot HTTP/WS 连接并处理四种通信模式。
- **AstrBot**：`astrbot_plugin/main.py` 是 AIOCQHTTP 插件壳，由 AstrBot 提供事件与协议 API。

两个入口都通过 `MaaWorkflowRuntime` 装配相同的服务、LLM、历史、追问、仓库、附件和报告组件。
插件模式不需要另起裸 OneBot 进程。

## 分层与导入边界

```text
入口 / 平台适配器
        │  PlatformEvent + PlatformPort
        ▼
maa_analyzer/runtime.py       共享运行时装配
        │
        ├── maa_analyzer/core/       配置、数据、模型、LLM、token、视觉
        └── maa_analyzer/features/   日志分析、指令、追问、历史、渲染、仓库
```

约束：

- 共享内核只能依赖 `maa_analyzer.platform` 中的 DTO 与 `PlatformPort`，不得 import
  `OneBotPlatformAdapter`、`AstrBotPlatformAdapter`、AstrBot `event/Context` 或 `aiocqhttp`。
- 平台适配器负责把原生事件转换为 `GroupMessageEvent` / `GroupUploadNotice`，并把发送、
  群文件和下载操作转换为 `PlatformPort` 方法。
- `MaaService` 通过注入的端口发送消息和访问群文件，不持有具体 Bot API。
- 所有消息发送结果统一为 `message_id: str | None`；文件上传结果为 `bool`。

## 目录结构

```text
OnebotMaaLogAnalyzer/
├── main.py                         # 裸 OneBot 启动器
├── astrbot_plugin/                 # AstrBot 插件壳
│   ├── main.py                     # Star 生命周期、事件过滤器、AIOCQHTTP 监听
│   ├── metadata.yaml
│   ├── _conf_schema.json           # 面板常用配置
│   └── requirements.txt
├── maa_analyzer/                   # 两个入口共享的唯一内核命名空间
│   ├── platform.py                 # PlatformPort、事件 DTO、发送者与文件元数据
│   ├── runtime.py                  # 共享运行时生命周期
│   ├── config.py                   # 配置加载、校验、面板覆盖、日志
│   ├── astrbot/
│   │   └── plugin_runtime.py       # 与 AstrBot 框架解耦的插件生命周期桥
│   ├── platforms/
│   │   ├── onebot/                 # HTTP/WS、适配器、事件桥
│   │   └── astrbot/                # AIOCQHTTP 事件与 API 适配器
│   ├── core/                       # models、service、data、LLM 等基础设施
│   └── features/maa/               # 分析、指令、历史、追问、渲染、仓库等业务
├── templates/                      # 图片报告模板，两个发布形态共用
├── debug/                          # 源码调试工具，不进入发布包
├── docs/
└── tools/                          # 编辑器工具，只进入裸模式普通包
```

`config.json` 不在源码仓库中提交，首次运行自动生成。运行时数据默认位于 `data/`；
AstrBot 默认使用 `data/plugin_data/OnebotMaaLogAnalyzer/`。

## 平台端口

`PlatformPort` 提供：

- 文本、消息段、图片和文件发送
- 群文件根目录/子目录列表、下载 URL 和上传
- 任意 URL 下载
- 协议端版本与群成员信息查询
- 运行时关闭/会话清理

共享 DTO 覆盖群上传通知、群消息、文件段、引用目标、发送者群角色和文件元数据。DTO 的
`to_service_payload()` 生成现有业务层可读的兼容结构，因此迁移不需要改写历史文件格式。

## 事件流转

| 平台事件 | 适配器输出 | 共享服务路由 |
| --- | --- | --- |
| OneBot `message/group` | `GroupMessageEvent` | `message.file` 或 `message.text` |
| OneBot `notice/group_upload` | `GroupUploadNotice` | `notice.group_upload` |
| AIOCQHTTP 群文本 | `GroupMessageEvent` | 指令或引用追问 |
| AIOCQHTTP 文件段 | `GroupMessageEvent` + 文件元数据 | 日志包分析 |
| AIOCQHTTP 群上传通知 | `GroupUploadNotice` | 日志包分析 |

AstrBot 在监听器执行前会先判断事件是否值得唤醒。`MaaEventFilter` 同步解析群号、命令、
文件名和引用消息，只让当前插件可能消费的事件通过；实际的监听群、启用状态和日志内容校验
仍由共享服务完成。

## 生命周期

`MaaWorkflowRuntime.start()` 幂等创建数据目录、LLM、服务、历史、分析器、追问存储、
监听器和后台任务。`stop()` 会：

1. 取消待处理的分卷计时和后台任务；
2. 保存服务配置、历史与任务状态；
3. 关闭运行时拥有的 LLM/HTTP 会话；
4. 关闭渲染浏览器；
5. 在入口拥有端口时关闭平台适配器。

AstrBot 插件额外负责隔离日志句柄、角色缓存和配置重载回滚。插件停用后再次启用会创建新的
运行时，不复用已取消的任务集合。

## 数据兼容

JSON 数据文件沿用原格式，并使用 `.tmp` 原子写入与恢复：

```text
data/
├── settings.json
├── ai_usage.jsonl
├── repo/
└── <配置名>/
    ├── groups.json
    ├── settings.json
    ├── project.json
    ├── commands.json
    ├── jobs/records.json
    └── history/
```

`config.json` 的 `bot.configs`、`llm`、`log` 等高级字段在两种入口中保持同一语义；
`onebot` 段只由裸入口消费。AstrBot 面板覆盖只在内存中修改有效配置，不回写原始文件。

## 真实联调边界

自动化测试覆盖端口转换、配置优先级、生命周期、发送/上传回退和裸 OneBot 四种通信模式的
配置兼容。真实 AstrBot 4.29.4 与 NapCat / Lagrange 的端到端安装、上传、分卷汇总、追问、
附件、历史清理和热重载仍需在部署环境联调。
