# Ze-nonebot

基于 NoneBot2 的 QQ 聊天机器人，支持自定义人设、群聊与私聊、长期记忆、图片理解和表情收藏。

[快速开始](#快速开始) · [使用指南](docs/usage.md) · [配置说明](docs/configuration.md) · [完整文档](docs/README.md)

核心功能由 `persona_agent` 插件提供，使用 OpenAI 兼容模型接口，通过外部 LLBot 的 OneBot V11 反向 WebSocket 接入 QQ。应用采用单进程与 SQLite，可部署到个人设备或 Linux 服务器。

## 功能

- **人设与对话**：分别配置群聊、普通私聊和管理员私聊风格，按白名单接入；支持明确呼叫、话题续聊和适度参与群聊。
- **来源明确的回复**：群聊按原消息分别处理，首条回复使用 QQ 原生引用，支持分段发送与合并转发理解。
- **长期记忆**：结合人物档案、关键词和本地向量检索，支持查看、记住、纠正和遗忘。
- **图片与表情**：理解图片、读取文字、追问多图；可选图片出处检索、QQ 收藏同步和本地表情库。
- **模型与技能**：为聊天、参与判断、媒体和记忆分配模型，按需加载 Markdown 技能。
- **实例维护**：统一配置与数据目录，提供 Linux systemd 服务、定时清理、备份及恢复工具。

## 快速开始

需要 **Python 3.13**、[uv](https://docs.astral.sh/uv/getting-started/installation/)、OpenAI 兼容的 Chat Completions 接口，以及已独立安装并登录的 [LLBot](https://luckylillia.com/)。

获取源码并进入项目目录：

```bash
git clone https://github.com/yyZe0122/Ze-nonebot.git
cd Ze-nonebot
```

以下命令均在项目根目录执行。

### 1. 安装依赖并初始化

Linux：

```bash
uv sync --project nonebot --locked --no-dev
nonebot/.venv/bin/python -B ops/init_instance.py
```

Windows PowerShell：

```powershell
uv sync --project nonebot --locked --no-dev
.\nonebot\.venv\Scripts\python.exe -B .\ops\init_instance.py
```

Windows x64 还需安装 [Visual C++ v14 运行库（x64）](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170)。仓库提供 Windows 前台启动入口，尚未完成 Windows 实际启动和业务验证；维护与恢复脚本用于 Linux。

### 2. 填写配置并连接 QQ

初始化会在 `config/` 中生成三个配置文件：

| 文件 | 必填内容 |
| --- | --- |
| `nonebot.env` | 监听地址、端口和 `ONEBOT_ACCESS_TOKEN` |
| `persona_agent.env` | `PERSONA_AGENT_API_KEY` |
| `persona_agent.yaml` | 模型地址与名称、管理员、群聊和私聊白名单 |

在 LLBot 中启用 OneBot V11 反向 WebSocket，连接：

```text
ws://<机器人服务地址>:18080/onebot/v11/ws
```

两侧的访问令牌须一致。默认监听 `127.0.0.1:18080`；跨主机或独立容器连接时，需要调整监听地址与网络配置。管理员也需要加入私聊白名单。详细步骤见[安装与连接](docs/getting-started.md)。

### 3. 启动机器人

Linux：

```bash
nonebot/.venv/bin/python -B ops/start_nonebot.py
```

Windows PowerShell：

```powershell
.\nonebot\.venv\Scripts\python.exe -B .\ops\start_nonebot.py
```

启动后，可在白名单群聊中 @ 机器人，或从白名单账号发起私聊。长期运行可按[运维指南](ops/OPERATIONS.md)安装 Linux 用户级服务。

> 已有实例请按[恢复流程](ops/OPERATIONS.md#恢复流程)处理数据库与当前遗忘账本。初始化只用于准备实例文件，不能代替数据恢复。

## 文档

| 文档 | 内容 |
| --- | --- |
| [安装与连接](docs/getting-started.md) | 环境要求、初始化、LLBot 接入和前台启动 |
| [配置说明](docs/configuration.md) | 配置加载、模型、权限、人设、预算与功能开关 |
| [使用指南](docs/usage.md) | 聊天方式、命令、续聊与失败提示 |
| [图片、转发与表情](docs/media.md) | 看图、查出处、多图选择、表情收藏与复核 |
| [记忆管理](docs/memory.md) | 记忆范围、本地向量、纠正与遗忘 |
| [技能扩展](docs/skills.md) | 内置技能、显式调用与自定义技能 |
| [架构说明](docs/architecture.md) | 消息处理、模型调度、存储和源码导航 |
| [运维指南](ops/OPERATIONS.md) | systemd、日志、保留规则、备份与恢复 |

## 项目结构

```text
.
├── nonebot/        # 应用入口、插件源码、依赖锁和配置模板
├── docs/           # 使用与开发文档
├── deploy/         # Linux 用户级 systemd 模板
└── ops/            # 初始化、启动、维护和恢复脚本
```

初始化后，配置写入 `config/`，持久数据写入 `state/`，缓存、日志和备份分别写入 `cache/`、`logs/`、`backups/`。仓库只提供中性人设和配置模板，不附带真实账号、密钥、聊天记录或预置记忆。

## 相关项目

- [NoneBot2](https://github.com/nonebot/nonebot2)：机器人框架。
- [LLBot](https://luckylillia.com/)：外部 QQ 接入服务，由使用者独立安装和维护。本项目不分发或管理 LLBot、PMHQ、QQ 客户端及其登录数据。
- [FastEmbed](https://github.com/qdrant/fastembed)：本地 CPU 向量模型。

基本消息收发使用 OneBot V11；QQ 收藏同步和上传额外依赖 LLBot WebUI 接口。其他 OneBot 实现的兼容性尚未验证。

## 开源声明

本项目采用 [MIT 许可证](LICENSE)，版权署名为 `Copyright (c) 2026 yyZe0122`。第三方组件保留各自的版权与许可声明。

本项目基于 [NoneBot2](https://github.com/nonebot/nonebot2) 开源机器人框架构建，在其基础上提供 `persona_agent` 插件、应用配置和部署运维工具。感谢 NoneBot Team 及社区贡献者。

NoneBot2 采用 MIT 许可证，原版权声明为 `Copyright (c) 2020 NoneBot Team`。框架及内置技能参考项目的来源、版权和完整许可原文见[第三方声明](THIRD_PARTY_NOTICES.md)。
