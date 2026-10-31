# 安装与连接

[文档首页](README.md) · [配置说明](configuration.md) · [运维指南](../ops/OPERATIONS.md)

本文介绍新实例的前台运行。已有数据库或运行数据的实例，应先阅读[恢复流程](../ops/OPERATIONS.md#恢复流程)。

## 环境要求

| 依赖 | 要求 |
| --- | --- |
| Python | 3.13，版本范围以 [pyproject.toml](../nonebot/pyproject.toml) 为准 |
| uv | 用于按 [uv.lock](../nonebot/uv.lock) 安装依赖，见[安装说明](https://docs.astral.sh/uv/getting-started/installation/) |
| 模型服务 | OpenAI 兼容 Chat Completions 接口；使用看图和表情分析时需支持图片输入 |
| QQ 接入 | 独立安装并登录的 LLBot，启用 OneBot V11 反向 WebSocket |

Linux 可使用前台入口或用户级 systemd 服务。Windows x64 提供前台入口，尚未完成实际启动和业务验证；`ops/maintain.py`、`ops/reconcile_forgetting.py` 和 systemd 定时任务用于 Linux。

Windows 还需安装 [Microsoft Visual C++ v14 运行库（x64）](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170)，供 ONNX Runtime 使用，见[系统要求](https://onnxruntime.ai/docs/install/#requirements)。初始化脚本不安装系统运行库。Windows 的 `tzdata` 随项目依赖安装；Linux 使用系统 IANA 时区数据。

## 安装依赖

下载源码并进入项目根目录。以下命令在 Linux shell 和 Windows PowerShell 中相同：

```bash
uv sync --project nonebot --locked --no-dev
```

依赖安装在 `nonebot/.venv/`。使用项目虚拟环境执行下文脚本。

## 初始化实例

Linux：

```bash
nonebot/.venv/bin/python -B ops/init_instance.py
```

Windows PowerShell：

```powershell
.\nonebot\.venv\Scripts\python.exe -B .\ops\init_instance.py
```

初始化会创建运行目录，并在缺失时复制：

| 模板 | 实例文件 |
| --- | --- |
| [nonebot/.env.example](../nonebot/.env.example) | `config/nonebot.env` |
| [nonebot/persona_agent.env.example](../nonebot/persona_agent.env.example) | `config/persona_agent.env` |
| [nonebot/persona_agent.yaml.example](../nonebot/persona_agent.yaml.example) | `config/persona_agent.yaml` |

全新实例还会创建空的 `state/nonebot/forget-ledger.jsonl`。已有配置和账本原样保留。初始化不启动机器人、不调用模型、不创建或迁移数据库，也不操作 LLBot 或安装 systemd 服务。

如果检测到已有状态、旧 `data/` 或 `nonebot/data/`、隔离素材、日志或备份，而当前遗忘账本缺失，初始化会停止。此时需要找回当前账本，不能用空文件替代。

## 填写配置

### 连接设置

在 `config/nonebot.env` 中设置监听地址、端口和访问令牌：

```dotenv
DRIVER=~fastapi+~httpx
HOST=127.0.0.1
PORT=18080
LOG_LEVEL=INFO
ONEBOT_ACCESS_TOKEN=replace-with-your-token
```

示例令牌必须替换为自己的值，并在 LLBot 连接端使用同一个令牌。

### 模型与权限

在 `config/persona_agent.env` 中填写实际模型密钥：

```dotenv
PERSONA_AGENT_API_KEY=replace-with-your-api-key
```

在已生成的 `config/persona_agent.yaml` 中修改下列字段，其余配置先保留模板值：

```yaml
model:
  base_url: "https://example.com/v1"
  api_key_env: PERSONA_AGENT_API_KEY
  model: "your-model"

access:
  owner_ids: [123456789]
  allowed_groups: [987654321]
  allowed_private_users: [123456789]
```

以上地址、模型名和号码均为占位示例。程序在 `base_url` 后追加 `/chat/completions`，因此填写服务商提供的 API 基础地址，不要重复填写完整调用路径。

`owner_ids` 只赋予管理权限，不会绕过白名单。管理员需要私聊管理时，也必须加入 `allowed_private_users`。群聊或私聊白名单为空时，不接入对应范围。

更多模型配置、人设和功能开关见[配置说明](configuration.md)。配置与新增技能文件修改后需重启。

## 接入 LLBot

按 [LLBot 官方文档](https://luckylillia.com/)独立安装服务并完成 QQ 登录，然后添加 OneBot V11 反向 WebSocket 连接：

```text
ws://<机器人服务地址>:18080/onebot/v11/ws
```

连接时注意：

- 同一主机、同一网络环境可以使用默认 `127.0.0.1`。
- LLBot 位于独立容器或另一台主机时，地址须从 LLBot 所在网络可达；容器内的 `127.0.0.1` 指向容器自身。
- 跨主机连接需要将 `HOST` 设置为可达的监听地址，例如 `0.0.0.0`，并配置端口访问范围与访问令牌。
- 如果修改了 `PORT`，WebSocket 地址中的端口也要同步修改。

LLBot、PMHQ、QQ 客户端及其登录数据由使用者维护，本项目不分发这些组件，也不接管它们的安装、升级或启停。

普通消息使用 OneBot V11。QQ 收藏表情同步与上传另需 LLBot WebUI 地址和令牌文件，见[表情收藏](media.md#表情收藏)；其他 OneBot 实现的兼容性尚未验证。

## 前台启动

Linux：

```bash
nonebot/.venv/bin/python -B ops/start_nonebot.py
```

Windows PowerShell：

```powershell
.\nonebot\.venv\Scripts\python.exe -B .\ops\start_nonebot.py
```

上述命令从项目根目录执行。使用解释器和脚本的绝对路径，也可从其他工作目录启动。日志写入 `logs/nonebot/nonebot.log`。

`ops/start_nonebot.py` 和直接执行 `nonebot/bot.py` 使用相同入口：先核对配置、受维护的数据路径、schema 版本和当前遗忘账本，再启动服务。正常启动不会补建缺失账本。数据库首次打开时会创建或迁移到当前 schema 5。

本地向量模型首次启动时在后台下载到 `cache/embedding-models/`。尚未就绪或加载失败时，记忆召回使用人物档案和关键词；详情见[本地向量](memory.md#本地向量)。

## 下一步

- 调整[人设与权限](configuration.md#权限与人设)，按[使用指南](usage.md)开始聊天。
- 按需启用[表情与搜图](media.md)或添加[技能](skills.md)。
- 需要后台常驻时，安装 [Linux 用户级服务](../ops/OPERATIONS.md#安装用户级服务)。
- 迁移或恢复实例前，核对[备份范围](../ops/OPERATIONS.md#备份范围)和当前遗忘账本。
