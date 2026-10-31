# NoneBot 应用

本目录包含 `persona_agent` 插件、NoneBot 启动入口、Python 依赖和配置模板。

[项目首页](../README.md) · [完整文档](../docs/README.md) · [安装与连接](../docs/getting-started.md) · [运维指南](../ops/OPERATIONS.md)

## 目录

| 路径 | 用途 |
| --- | --- |
| [bot.py](bot.py) | 统一配置检查、日志设置和 NoneBot 启动 |
| [src/plugins/persona_agent/](src/plugins/persona_agent/) | 聊天、模型、记忆、图片和表情处理 |
| [skills/](skills/) | 随项目提供的 Markdown 技能与参考资料 |
| [examples/persona.basic.yaml](examples/persona.basic.yaml) | 中性基础人设，复制 `persona` 段后使用 |
| [pyproject.toml](pyproject.toml)、[uv.lock](uv.lock) | Python 版本、依赖声明与锁定 |
| [tests/](tests/) | 现有测试代码 |
| `.venv/` | 在本机生成的 Python 环境 |

## 配置与运行

从项目根目录按[安装与连接](../docs/getting-started.md)安装依赖并初始化。运行配置存放于项目根目录的 `config/`，持久数据存放于 `state/nonebot/`。

- [.env.example](.env.example) → `config/nonebot.env`
- [persona_agent.env.example](persona_agent.env.example) → `config/persona_agent.env`
- [persona_agent.yaml.example](persona_agent.yaml.example) → `config/persona_agent.yaml`

这些模板由 `ops/init_instance.py` 复制；基础人设示例不会自动加载。前台与 systemd 均使用 `ops/start_nonebot.py`，直接执行 `bot.py` 也保留相同的配置、路径和账本检查。

功能说明已按主题整理到 [docs/](../docs/README.md)：[配置](../docs/configuration.md)、[使用](../docs/usage.md)、[媒体](../docs/media.md)、[记忆](../docs/memory.md)、[技能](../docs/skills.md)和[架构](../docs/architecture.md)。
