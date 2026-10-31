# 配置说明

[文档首页](README.md) · [安装与连接](getting-started.md) · [完整配置模板](../nonebot/persona_agent.yaml.example)

先运行初始化脚本，再编辑实例的 `config/` 目录。本文说明配置关系与常用参数；字段类型、范围和省略字段时的默认值见 [config.py](../nonebot/src/plugins/persona_agent/config.py)。

## 配置文件与加载顺序

| 文件或变量 | 作用 |
| --- | --- |
| `config/nonebot.env` | NoneBot 监听地址、端口、驱动与 OneBot 访问令牌 |
| `config/persona_agent.yaml` | 模型、权限、人设和功能配置 |
| YAML 同目录的 `persona_agent.env` | 模型及图片检索的密钥环境变量 |
| `state/nonebot/active_model.txt` | `/model` 保存的模型选择；名称仍有效时优先于 YAML 中的选择 |
| `PERSONA_AGENT_CONFIG` | 覆盖 YAML 位置，相对路径按项目根目录解析 |
| `PERSONA_LOG_DIR` | 覆盖应用日志目录，相对路径按项目根目录解析，默认 `logs/nonebot` |

启动固定读取 `config/nonebot.env`，不会额外合并当前工作目录的 `.env`。YAML、连接配置和 YAML 同目录的密钥文件都必须存在。

配置文件修改后需重启。管理员私聊 `/model <名称>` 可以切换已配置的模型，同时重新读取 YAML 同目录的密钥文件；它不会重新加载 YAML。普通启动加载密钥文件时，已有进程环境变量优先；模型切换时重新加载的文件值会覆盖同名环境变量。

## 路径约定

YAML 中的相对路径以 **YAML 所在目录** 为基准。默认文件位于 `config/`，对应路径如下：

| 配置项 | 模板值 | 实际目录或文件 |
| --- | --- | --- |
| `database_path` | `../state/nonebot/persona_agent.sqlite3` | 主数据库 |
| `stickers.directory` | `../state/nonebot/stickers` | 本地收藏原图 |
| `stickers.quarantine_directory` | `../cache/quarantine/stickers` | 表情隔离目录 |
| `skills.directory` | `../nonebot/skills` | 技能目录 |
| `memory.embedding.cache_directory` | `../cache/embedding-models` | 向量模型缓存 |
| `stickers.webui_token_file` | `llbot_webui_token.txt` | 可选的 LLBot WebUI 令牌文件 |

启动时会核对数据库、表情原图和隔离目录是否指向项目维护脚本约定的位置。即使使用 `PERSONA_AGENT_CONFIG`，这三个实际位置也必须保持一致；调整配置位置后应相应修改相对路径。

初始化不会生成 LLBot WebUI 令牌文件。该文件、自定义位置的 YAML 及其密钥文件不在默认备份清单中，详见[备份范围](../ops/OPERATIONS.md#备份范围)。

## 模型

### 单模型

模板使用 `model` 配置，内部名称为 `default`：

```yaml
model:
  base_url: "https://example.com/v1"
  api_key_env: PERSONA_AGENT_API_KEY
  model: "your-model"
  timeout_seconds: 123
  temperature: 0.7
  supports_images: true
  max_images_per_message: 8
  image_timeout_seconds: 15
```

程序在 `base_url` 后追加 `/chat/completions`，发送非流式请求。`api_key_env` 填环境变量名，实际密钥放在 `persona_agent.env`。所选模型需要支持消息输入及 JSON 决策输出；看图还需要实际支持图片输入。

设置 `supports_images: false` 或 `max_images_per_message: 0` 可关闭该模型的图片输入。图片上限作用于整轮，包含引用、连续多图和转发图片。

### 多模型与任务分工

需要多个模型时，将模板中的 `model` 块替换为 `models` 与 `active_model`，并按需修改现有 `model_tasks`：

```yaml
active_model: chat

models:
  chat:
    base_url: "https://example.com/v1"
    api_key_env: PERSONA_AGENT_API_KEY
    model: "your-vision-model"
    supports_images: true
  light:
    base_url: "https://example.com/v1"
    api_key_env: PERSONA_AGENT_API_KEY
    model: "your-text-model"
    supports_images: false

model_tasks:
  participation:
    model: light
  media:
    model: chat
  memory:
    model: light
```

| 用途 | 选择方式 |
| --- | --- |
| 聊天 | `active_model`，或管理员用 `/model` 保存的选择 |
| 普通群聊参与判断 | `model_tasks.participation` |
| 表情入库分析与复核 | `model_tasks.media` |
| 后台记忆整理 | `model_tasks.memory` |
| 本地语义向量 | `memory.embedding`，使用 CPU，不调用上述聊天接口 |

任务的 `model` 留空时跟随当前聊天模型，也可分别设置 `max_tokens`、`timeout_seconds` 和 `daily_token_budget`。表情复核共用媒体任务配置。

管理员在白名单私聊中使用 `/model` 查看模型与绑定，使用 `/model <名称>` 切换。正在处理的一轮保留自己的模型与凭据快照，新请求采用新选择。并发、期限与重试细节见[模型调度](architecture.md#模型调度与失败处理)。

## 权限与人设

| 配置 | 作用 |
| --- | --- |
| `access.owner_ids` | 管理员账号，可使用私聊管理命令及管理员预算 |
| `access.allowed_groups` | 允许接入的群 |
| `access.allowed_private_users` | 允许接入的私聊账号，管理员也须列入 |
| `persona.name`、`persona.aliases` | 机器人名称与呼叫别名 |
| `persona.profile`、`personality`、`interests` | 基础身份、性格和兴趣 |
| `persona.group` | 群聊规则与示例 |
| `persona.private` | 普通私聊规则与示例 |
| `persona.owner_private` | 管理员私聊规则与示例 |
| `safety.rules` | 加入模型提示的行为约束，不能替代代码中的权限检查 |

白名单为空时，不接入相应群聊或私聊；未获准消息写入有保留期限的审计记录。管理员身份与接入权限独立。

[基础人设模板](../nonebot/examples/persona.basic.yaml)使用中性身份，没有预设性别、私人关系、个人经历或兴趣。将其中的 `persona` 段复制到实例配置即可，示例文件不会自动加载。`assistant_history_since` 可限制使用的历史机器人发言时间，模板为 `null`。

## 预算与频率

| 配置 | 模板值 | 含义 |
| --- | --- | --- |
| `budget.context_token_budget` | 4096 | 文本上下文预算，按字符近似分配 |
| `budget.max_reply_tokens` | 512 | 聊天输出上限，包含 JSON 决策格式 |
| `budget.daily_token_budget` | 100000 | 普通用户每日模型用量阈值 |
| `budget.owner_daily_token_budget` | 300000 | 管理员每日模型用量阈值 |
| `budget.max_concurrent_replies` | 2 | 共享模型调用并发上限 |
| `budget.max_batch_wait_seconds` | 4 | 连续输入与补图的最大批次等待 |
| `scheduler.chat_timeout_seconds` | 123 | 本轮聊天流程共用的模型调用期限 |
| `group_behavior.cooldown_seconds` | 60 | 普通群聊发言冷却 |
| `group_behavior.consideration_interval_seconds` | 30 | 普通群聊参与判断的起始间隔 |
| `group_behavior.silence_backoff_max_seconds` | 120 | 连续沉默后的最大判断间隔 |
| `group_behavior.followup_seconds` | 90 | 同一用户近期对话的候选窗口 |
| `group_behavior.max_recent_share` | 0.2 | 近期群聊发言占比限制 |

每日 token 阈值按 UTC 日统计，0 表示不限制；请求发出前检查已有用量，已发出的请求可能使最终用量超过阈值，因此不能将它视为严格费用上限。图片 token 不在本地字符预算中精确计费。

模型任务还可设置自己的每日 token 预算。图片出处检索有独立的次数限额，其 `daily_limit: 0` 表示停用检索；不要与 token 预算的 0 混淆。

## 可选功能

| 配置 | 模板状态 | 说明 |
| --- | --- | --- |
| `skills.enabled` | 开启 | 默认加载 `natural-chat`，见[技能扩展](skills.md) |
| `image_sources.enabled` | 开启 | SauceNAO、SerpApi 需要各自密钥；trace.moe 使用匿名接口，见[图片出处检索](media.md#图片出处检索) |
| `stickers.enabled` | 关闭 | 开启后收集平台标记的表情；WebUI 不可用时使用本地目录 |
| `stickers.review_enabled` | 开启 | 仅在表情功能开启时生效 |
| `private_proactive.enabled` | 关闭 | 开启后对已有白名单私聊按计划判断是否主动发言 |

主动私聊的首次检查间隔由 `initial_check_minutes` 控制，模板为 5 分钟；失败后的检查间隔为 `error_retry_minutes`，模板为 15 分钟。实际是否发言及下一次检查时间由模型决策，不代表每隔固定时间发送消息。

## 模板值与代码默认值

初始化会复制完整模板，因此新实例采用模板中的设置。自己编写精简 YAML 时，省略字段会使用代码默认值，以下差异需要留意：

| 配置 | 模板值 | 省略时的代码默认值 |
| --- | --- | --- |
| `skills.enabled` | `true` | `false` |
| `image_sources.enabled` | `true` | `false` |
| `group_behavior.cooldown_seconds` | 60 | 90 |
| `group_behavior.max_recent_share` | 0.2 | 0.25 |
| `memory.recent_message_limit` | 8 | 24 |

其余配置按[完整模板](../nonebot/persona_agent.yaml.example)与 [config.py](../nonebot/src/plugins/persona_agent/config.py)核对。文件与日志保留策略另见[运维指南](../ops/OPERATIONS.md#保留规则)。
