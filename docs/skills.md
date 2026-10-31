# 技能扩展

[文档首页](README.md) · [使用指南](usage.md) · [配置说明](configuration.md)

技能是可按需加入模型上下文的 Markdown 说明和参考资料。当前实现支持说明型技能，不执行技能脚本，也不提供外部工具运行环境。

## 内置技能

| 技能 | 用途 | 默认加载 |
| --- | --- | --- |
| [natural-chat](../nonebot/skills/natural-chat/SKILL.md) | 自然接话，减少复述、固定调侃与例行收尾 | 是 |
| [concise-answer](../nonebot/skills/concise-answer/SKILL.md) | 具体问答、比较、解释与操作步骤 | 否 |
| [chinese-writing](../nonebot/skills/chinese-writing/SKILL.md) | 新写、续写文章与故事，按需读取叙事或非虚构参考 | 否 |
| [text-polish](../nonebot/skills/text-polish/SKILL.md) | 润色、压缩和局部改写，保留事实与限定条件 | 否 |
| [image-reading](../nonebot/skills/image-reading/SKILL.md) | 理解画面、文字与图表，区分观察和出处检索 | 否 |

模板开启技能功能，并将 `natural-chat` 设为默认项。管理员在白名单私聊中使用 `/skills` 或 `/技能` 查看目录及不可用项。

## 调用技能

在白名单聊天中，以 `$技能名` 开头显式指定：

```text
$chinese-writing 写一段产品说明
$text-polish 压缩下面这段话，保留原来的事实
```

普通聊天直接加载默认技能。模型还会看到简短目录，必要时返回 `activate_skills`；程序读取所选正文和参考资料后，再请求最终回复。选择技能的中间结果不发送到 QQ。

每轮最多进行一次模型驱动的技能扩展。正常流程是一轮聊天生成，或“选择技能 + 最终生成”两轮；格式纠正和模型重试可能增加实际调用次数，仍共用本轮模型与凭据快照及聊天期限。详见[模型调度](architecture.md#模型调度与失败处理)。

## 配置

```yaml
skills:
  enabled: true
  directory: ../nonebot/skills
  default_skills: [natural-chat]
  enabled_skills: []
  max_active: 3
  max_prompt_chars: 3000
```

`directory` 相对 YAML 所在目录解析。`enabled_skills` 为空时发现目录中的全部技能，也可列出允许启用的名称；它与默认加载列表是不同设置。

默认最多同时加载 3 个技能，正文、参考资料和目录共同受字符预算约束。正文预算同时受 `max_prompt_chars` 与上下文预算限制，多个技能叠加后也可能超限。

新增技能和配置变更在重启后生效。单个技能损坏或超限时记录原因并跳过；默认技能不可用会导致启动失败，避免缺失默认规则。

## 添加技能

在技能根目录新建子目录，提供带 YAML frontmatter 的 `SKILL.md`：

```text
nonebot/skills/my-skill/
├── SKILL.md
└── references/
    └── guide.md      # 可选
```

`SKILL.md` 示例：

```markdown
---
name: my-skill
description: 将用户提供的材料整理为简短、清晰的步骤。
---

保留原材料中的事实、条件和限制。
先说明结果，再按执行顺序列出必要步骤。
材料不足时指出具体缺少的信息，不补写未提供的事实。
```

名称必须与目录一致，使用小写字母、数字和单连字符，最长 64 字符。`description` 必填，长度为 1～1024 字符；正文不能为空。Markdown 文件大小上限为 64 KiB，正文还需满足配置的字符预算。

参考资料放在 `references/` 下，发现时最多登记 32 个 Markdown 文件；每个技能选择项最多请求 3 份参考资料，只能读取已登记且位于该技能目录内的文件。日志记录实际加载的技能、参考文件及内容哈希版本。

格式采用 [Agent Skills](https://agentskills.io/specification) 的名称、描述和正文组织方式，但这里只实现说明与 Markdown 参考资料加载。声明依赖 `allowed-tools` 的技能不可用；文件被发现不代表其需要的工具已经存在。技能也不能替代访问权限、目标校验和真实工具结果。

## 设计参考

内置写作技能参考 [Humanizer](https://github.com/blader/humanizer)、[Humanizer-zh](https://github.com/ai-zixun/humanizer-zh) 和 [Stop Slop](https://github.com/hardikpandya/stop-slop) 的编辑思路，按 QQ 场景整理为简短规则。技能正文与参考资料位于 [nonebot/skills/](../nonebot/skills/)。

这些参考项目的版权与许可原文见[第三方声明](../THIRD_PARTY_NOTICES.md#内置技能参考项目)。
