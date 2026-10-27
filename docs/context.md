# 上下文与提示词缓存

本文说明 Bot 如何组织模型请求，让 provider 的**提示词前缀缓存**尽量命中，
以及在输入超过模型窗口时如何自动压缩。

## 提示词缓存是什么

大多数 OpenAI 兼容端点（DeepSeek、OpenAI 等）会对请求做**前缀缓存**：
当本次请求的 `messages` / `tools` 开头与之前某次请求逐字节一致时，
provider 直接复用已计算的 KV，只按缓存读取价计费。

命中率取决于「前缀是否稳定」：

- 系统提示词、工具定义、历史消息都不能在会话中途改写；
- 新增内容只能**追加**在末尾；
- 任何在开头附近的改动（哪怕只改一个字符）都会让之后的缓存全部失效。

DeepSeek 以 64 token 为缓存块，并返回：

```json
{ "usage": { "prompt_tokens": 12000,
             "prompt_cache_hit_tokens": 11000,
             "prompt_cache_miss_tokens": 1000 } }
```

## Bot 的缓存友好做法

| 做法 | 位置 |
| --- | --- |
| 系统提示词在会话内保持不变 | `MaaAnalyzer._system_prompt` |
| 追问复用分析阶段的系统提示词与工具说明，追问指令放到末尾的 user 消息 | `_answer_followup_inner` |
| 追问上下文以**原始日志摘要开头**，文件名等独有信息放在末尾 | `_build_followup_context` |
| 工具循环只追加消息；不再每轮折叠/重写历史 | `core/compaction.py` |
| 收敛与截断重试保留 `tools` 定义，仅用 `tool_choice="none"` 禁止调用 | `_run_tool_loop` / `_retry_truncated` |
| 首轮输入在会话开始时按预算裁剪一次，之后不再改动 | `ConversationCompactor.prepare` |
| 输入预算预留输出 token，避免「刚好塞满」被 400 拒绝 | `LLMPool.input_budget` |

> 追问如果由「重启后从历史归档恢复」，系统提示词与工具集合应与原分析一致；
> 若配置已被修改，则只能部分命中，这是预期行为。

## 自动压缩

### 触发条件

1. **预算压力**：估算输入超过 `输入预算 × context_compact_ratio`（默认 80%）。
2. **端点超限**：provider 返回上下文超限（400）时，强制压缩后重试，最多 2 次。

输入预算 = 上下文窗口 − `max_tokens`（本次最大输出）− 安全余量，
因此不会出现「输入刚好等于窗口、输出没有空间」的情况。

### 压缩步骤

1. 裁剪**历史工具结果**（保留最近 2 条），单条上限 `context_prune_tool_result_tokens`；
2. 若仍超预算，把「首条 user 之后、最近对话之前」的中间区段交给模型摘要；
3. 摘要以一条 `user` 消息插回历史，保留：
   - 系统提示词；
   - 首轮日志摘要（模型不会“失忆”）；
   - 最近约 `context_retain_ratio`（默认 25%）的对话。
4. 仍超预算时，从最旧的对话轮次开始整轮丢弃。

摘要请求本身按块进行，单块不超过预算的一半，最多 4 块，避免压缩调用自己超限。
摘要失败时只保留工具结果裁剪，绝不丢消息。

### 相关设置

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| `context_compact_enabled` | `true` | 是否启用自动压缩 |
| `context_compact_ratio` | `0.8` | 触发压缩的预算占比（0.5–0.95） |
| `context_retain_ratio` | `0.25` | 压缩后保留最近对话的比例（0.1–0.5） |
| `context_prune_tool_result_tokens` | `512` | 历史工具结果裁剪上限；`0` = 不裁剪 |
| `context_summary_tokens` | `1500` | 摘要的目标 token 上限 |

### 代价

- 每次真正压缩都会**改写一次**历史前缀，导致该次请求缓存失效；
  之后继续追加，缓存重新累积。
- 摘要本身是一次模型调用，会计入 `*-compact` 阶段的用量；
  因此压缩阈值不宜过低（默认 80%）。

## 观测

- 日志：每次成功调用输出 `缓存命中 N%（读取 X / 未命中 Y）`。
- `/maa ai`：每个会话显示 `缓存命中 N%`。
- 用量文件：`<data_dir>/ai_usage.jsonl` 新增
  `uncached_prompt_tokens` / `cache_read_tokens` / `cache_write_tokens` 字段。

## 验证

- 同一会话内，工具循环第 2 次及之后的请求应命中缓存（`cache_read_tokens > 0`）。
- 追问的第一条请求应能复用分析阶段的系统提示词与日志摘要前缀。
- 输入超过窗口时应看到 `[压缩] 摘要压缩：... → ... tokens`，
  且最终请求成功、首条 user 仍存在。
