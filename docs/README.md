# 文档

[返回项目首页](../README.md)

首次使用从[安装与连接](getting-started.md)开始；已运行的实例可直接查看[使用指南](usage.md)或[运维指南](../ops/OPERATIONS.md)。

## 使用与配置

| 文档 | 解决的问题 |
| --- | --- |
| [安装与连接](getting-started.md) | 如何准备 Python 环境、初始化配置并连接 LLBot |
| [配置说明](configuration.md) | 如何选择模型、设置白名单、人设和预算 |
| [使用指南](usage.md) | 如何呼叫机器人、使用命令和重置私聊 |
| [图片、转发与表情](media.md) | 如何看图、选择多图、查出处和管理表情 |
| [记忆管理](memory.md) | 记忆如何建立、在哪些会话可见、如何纠正或遗忘 |
| [技能扩展](skills.md) | 如何调用内置技能或添加自己的 Markdown 技能 |

## 开发与维护

| 文档 | 内容 |
| --- | --- |
| [架构说明](architecture.md) | 请求处理流程、模型调度、数据存储与源码入口 |
| [运维指南](../ops/OPERATIONS.md) | Linux 服务、定时任务、日志、清理、备份与恢复 |
| [应用目录说明](../nonebot/README.md) | `nonebot/` 的职责及配置模板位置 |

## 配置模板

| 文件 | 用途 |
| --- | --- |
| [完整配置](../nonebot/persona_agent.yaml.example) | 模型、权限、人设和各功能参数 |
| [模型与搜图密钥](../nonebot/persona_agent.env.example) | 环境变量名称，不含真实密钥 |
| [OneBot 连接配置](../nonebot/.env.example) | 监听地址、端口与访问令牌 |
| [基础人设](../nonebot/examples/persona.basic.yaml) | 可复制到实例配置中的 `persona` 段 |
| [维护策略](../ops/lifecycle.json) | 文件、日志、备份的保留期限与容量阈值 |

文档中的“模板值”指初始化时复制的配置；省略字段时使用的代码默认值可能不同，详见[配置说明](configuration.md#模板值与代码默认值)。运行配置和历史数据保存在实例目录，不属于文档或发布内容。
