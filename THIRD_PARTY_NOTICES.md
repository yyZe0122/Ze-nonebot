# 第三方声明

本文件记录项目使用的第三方框架及内置技能参考项目的来源和许可。第三方组件的版权归各自作者，其许可证独立适用。

## NoneBot2

本项目基于 NoneBot2 构建，通过 Python 依赖 `nonebot2[fastapi,httpx]` 使用该框架，并在其基础上提供应用入口、`persona_agent` 插件和部署运维工具。

- 上游项目：[nonebot/nonebot2](https://github.com/nonebot/nonebot2)
- 官方文档：[nonebot.dev](https://nonebot.dev/)
- 当前锁定版本：2.5.0，见 [nonebot/uv.lock](nonebot/uv.lock)。
- 许可证：MIT。
- 原版权声明：`Copyright (c) 2020 NoneBot Team`。
- 完整许可原文：[本地副本](licenses/NoneBot2-LICENSE.txt)，保留自 [NoneBot2 v2.5.0 LICENSE](https://github.com/nonebot/nonebot2/blob/v2.5.0/LICENSE)。

感谢 NoneBot Team 及社区贡献者提供和维护该开源框架。

## 内置技能参考项目

内置写作技能参考以下项目的编辑思路，并按 QQ 聊天场景整理为简短规则，具体用途见[技能文档](docs/skills.md#设计参考)。以下项目均采用 MIT 许可证，保留其原始版权声明与许可文本。

| 项目 | 原版权声明 | 完整许可原文 |
| --- | --- | --- |
| [Humanizer](https://github.com/blader/humanizer) | Copyright (c) 2025 Siqi Chen | [本地副本](licenses/Humanizer-LICENSE.txt) · [上游原文](https://github.com/blader/humanizer/blob/main/LICENSE) |
| [Humanizer-zh](https://github.com/ai-zixun/humanizer-zh) | Copyright (c) 2026 aizixun | [本地副本](licenses/Humanizer-zh-LICENSE.txt) · [上游原文](https://github.com/ai-zixun/humanizer-zh/blob/main/LICENSE) |
| [Stop Slop](https://github.com/hardikpandya/stop-slop) | Copyright (c) 2025 Hardik Pandya | [本地副本](licenses/Stop-Slop-LICENSE.txt) · [上游原文](https://github.com/hardikpandya/stop-slop/blob/main/LICENSE) |
