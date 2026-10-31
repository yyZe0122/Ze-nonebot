# 运维指南

[文档首页](../docs/README.md) · [安装与连接](../docs/getting-started.md) · [配置说明](../docs/configuration.md) · [架构说明](../docs/architecture.md)

本文适用于 Linux，以部署账号执行命令。示例根目录为 `$HOME/bots/qqbot`；选择其他位置时，应同步调整各 systemd 服务文件的路径。

维护脚本只处理本项目的配置、数据库、记忆、素材、日志和备份。LLBot、PMHQ、QQ 客户端及其登录状态由使用者在外部环境独立维护。

## 目录职责

| 路径 | 用途 |
| --- | --- |
| `nonebot/` | 应用源码、配置模板、依赖锁、现有测试及本机生成的虚拟环境 |
| `docs/` | 使用、配置与开发文档 |
| `deploy/` | 用户级 systemd 服务和定时器模板 |
| `config/` | 实例 YAML、环境变量与可选令牌文件 |
| `state/nonebot/` | 主数据库、当前模型选择、遗忘账本和收藏原图 |
| `state/maintenance/` | 最近维护报告和 8 天容量采样 |
| `cache/tmp/` | 可再生成的临时文件 |
| `cache/quarantine/stickers/` | 等待恢复或到期清理的表情原图 |
| `cache/embedding-models/` | 可重新下载的本地向量模型 |
| `logs/nonebot/`、`logs/ops/` | 应用日志与维护执行记录 |
| `backups/nonebot/` | 数据库快照、配置、账本和持久素材备份 |
| `ops/` | 初始化、启动、维护、恢复脚本与保留策略 |

`config/`、`state/`、`cache/`、`logs/`、`backups/`、真实环境文件、YAML 和虚拟环境均在仓库排除规则中。运行文件应使用上述职责明确的目录。

YAML 相对路径以配置文件所在目录为基准。启动会核对数据库、收藏原图和隔离目录与维护脚本约定一致，具体路径见[配置说明](../docs/configuration.md#路径约定)。

## 首次初始化

按[安装与连接](../docs/getting-started.md)同步依赖、运行 `ops/init_instance.py` 并填写配置。初始化创建缺失目录与模板，仅为全新实例创建空遗忘账本，不启动机器人、迁移数据库或安装服务。

已有运行数据但当前账本缺失时，初始化会拒绝继续。应使用[恢复流程](#恢复流程)找回账本，不能用空账本替代。

本机和 systemd 共用 `ops/start_nonebot.py`，直接执行 `nonebot/bot.py` 也保留配置、路径、schema 和账本检查。Windows 前台命令及尚未实机验证的范围见[安装文档](../docs/getting-started.md#环境要求)；下述维护、恢复和 systemd 操作用于 Linux。

## 安装用户级服务

以下步骤用于首次安装本项目用户级服务。已有同名单元时，先合并自己的路径和环境设置，再更新模板。

### 1. 安装单元文件

模板使用 `%h/bots/qqbot`，即部署账号的 `$HOME/bots/qqbot`。先完成路径调整，再执行：

```bash
cd "$HOME/bots/qqbot"
mkdir -p "$HOME/.config/systemd/user"
install -m 600 deploy/nonebot.service deploy/qqbot-maintenance.service deploy/qqbot-maintenance.timer deploy/qqbot-backup.service deploy/qqbot-backup.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
```

### 2. 启动应用

```bash
systemctl --user enable --now nonebot.service
systemctl --user status nonebot.service
tail -n 50 logs/nonebot/nonebot.log
```

服务从 `config/` 读取配置，使用 `nonebot/.venv/` 中的解释器启动。配置齐全、数据库已创建后，再启用维护定时器。

### 3. 启用定时任务

```bash
systemctl --user enable --now qqbot-maintenance.timer qqbot-backup.timer
systemctl --user list-timers 'qqbot-*'
```

如需退出登录后继续运行，由主机管理员为部署账号启用 linger。用户级服务的持续运行也取决于主机的用户会话配置。

| 单元 | 作用 |
| --- | --- |
| `nonebot.service` | 主服务；失败后重启，停止期限为 90 秒 |
| `qqbot-maintenance.timer` | 北京时间每小时第 15 分钟附近执行维护 |
| `qqbot-backup.timer` | 北京时间每日 04:30 附近备份 |

两个定时器最多随机延迟 60 秒，`Persistent=true` 会补执行关机期间错过的任务。维护与备份通过文件锁避免重叠；已有任务占用锁时，本次跳过。

## 日常操作

按需要选择操作：

| 操作 | 命令 |
| --- | --- |
| 查看状态 | `systemctl --user status nonebot.service` |
| 重启应用 | `systemctl --user restart nonebot.service` |
| 停止应用 | `systemctl --user stop nonebot.service` |

配置修改后需重启。停机先停止接收并结束后台任务，队列最多等待 65 秒，之后取消剩余任务；systemd 的停止期限为 90 秒。发送结果未知的消息不会在重启后自动补发。

维护入口从项目根目录执行：

| 命令 | 实际行为 |
| --- | --- |
| `python3 -B ops/maintain.py clean` | 清理到期临时文件、隔离素材、日志和备份，并写报告 |
| `python3 -B ops/maintain.py backup` | 创建一致快照和素材备份，轮转到期备份并写报告 |
| `python3 -B ops/maintain.py inspect` | 统计容量，更新采样与报告 |

这些命令没有模拟运行含义，`inspect` 也会写入报告。脚本不启动机器人或调用模型。维护使用 Linux 文件锁，归属标记为 `.qqbot-root`；清理拒绝越出项目根目录或进入符号链接、挂载的备份子树。

文件保留策略位于 [lifecycle.json](lifecycle.json)，修改后下一次维护生效。数据库保留参数位于 `config/persona_agent.yaml`，需重启应用生效。

## 日志与诊断

查看已有状态与最近报告：

```bash
cd "$HOME/bots/qqbot"
systemctl --user status nonebot.service
systemctl --user status qqbot-maintenance.service qqbot-backup.service
systemctl --user list-timers 'qqbot-*'
cat state/maintenance/clean-latest.json
cat state/maintenance/backup-latest.json
tail -n 50 logs/nonebot/nonebot.log
```

报告在对应维护任务运行后生成。维护失败会记录错误，并以非零退出码反映在 systemd 状态中。服务正常运行只表明运行状态，具体消息或功能是否成功仍需查看对应记录。

| 现象 | 优先查看 |
| --- | --- |
| 未回复 | 白名单、原消息记录、参与判断理由、队列状态和模型结果 |
| 图片未加载或选错 | 引用链、补图关联、图片选择编号、读取状态和模型图片能力 |
| 模型超时或失败 | `request_id`、用途、排队与请求耗时、尝试次数及 HTTP 状态 |
| 回复缺段或疑似重复 | `reply_turns/reply_parts` 的逐段回执与 `sent/unknown` 状态 |
| QQ 收藏失败 | 列表同步状态、原生上传失败次数与本地文件状态 |
| 记忆未召回 | 可见范围、有效版本、人物/关键词结果和本地向量加载状态 |

按 `request_id` 关联同一请求，结合 `raw_id` / `source_message_id` 和图片键追踪“原消息 → 参与判断 → 上下文与媒体 → 模型 → 发送”。参与日志还包含续聊窗口、冷却、退避与实际续聊判断。

`model_calls` 保存每次实际尝试的用量、耗时和失败原因。缺少供应商用量时标记 `estimated=true`，失败占位的 0 token 不能证明没有消耗。模型用量和搜图日限额按 UTC 日计，表情复核次数按北京时间自然日计。

聊天决策解析失败日志包含校验位置、结束原因、输出长度及**模型输出前 200 字符**；不能视作完全不含模型文本。图片下载错误记录类别和 HTTP 状态，QQ 接口错误记录阶段和数字业务码。分享日志前，应检查其中的用户内容和模型输出片段。

## 保留规则

数据库到期清理由应用每 30 分钟执行一次；文件和备份清理由维护任务执行。

| 内容 | 默认或模板规则 |
| --- | --- |
| 原始消息 | 14 天，由配置与记录的 `expires_at` 控制 |
| 关闭的私聊会话 | 关闭满 24 小时后删除原文、片段和会话记录 |
| 会话片段、审计记录 | 30 天 |
| 模型用量日报 | 90 天，可通过 YAML 调整 |
| 未收藏图片识别元数据 | 超过 30 天且无保留消息引用时删除 |
| 长期记忆及最短证据、收藏目录和淘汰指纹 | 不因原始聊天到期自动删除 |
| 媒体复核日志 | 沿用 `observed_media_retention_days`，模板为 30 天；最后复核原因保留在目录中 |
| 自动停用表情原图 | 等有效聊天引用失效、文件满 24 小时后隔离，隔离满 7 天删除 |
| 管理员手动停用原图 | 保留，不因停用进入自动清理 |
| `cache/tmp/`、本地表情下载残留 `.tmp` | 超过 24 小时清理 |
| 无引用的本地表情原图 | 文件满 24 小时后隔离，隔离满 7 天删除；恢复引用时恢复文件 |
| 应用日志 | 14 天、约 200 MiB；当前日志受保护 |
| 应用单日志 | 10 MB 轮转并压缩旧日志，总量由小时任务约束 |
| 运维日志 | 30 天、20 MiB；当天日志受保护 |
| 每日备份 | 7 天，按清单创建时间轮转 |
| 未完成备份 | `backups/nonebot/` 下的未完成副本超过 24 小时清理 |

正在写入的日志可暂时超过预算，维护报告会记录。系统 journal、共享 uv/pip 缓存和其他应用的数据不在维护范围内；旧 `backups/daily/`、`backups/deploy/` 也不读取或清理。

容量统计累计 `state/nonebot/` 与 `state/maintenance/`，超过 5 GiB 或所在文件系统空闲不足 15% 时写本机报告，不向聊天发送通知。SQLite 删除后的空闲页可复用，脚本不自动执行 `VACUUM`。

### 表情隔离

内容复核在 NoneBot 后台完成，维护脚本只负责文件生命周期。使用次数只影响检查顺序，不直接决定淘汰，详见[表情复核](../docs/media.md#表情复核与原图清理)。

自动停用原图的处理要求 `auto_retired_at` 已设置、当前未启用且没有有效聊天引用。原路径和 `quarantine_path` 记录在数据库中，隔离期限从移动时开始计算。恢复引用时恢复文件，但不自动启用表情；到期删除文件后仍保留哈希与淘汰理由。

移动、恢复和删除通过 SQLite 写事务互斥，并校验目录与哈希文件名。文件移动与事务提交之间中断时，下次维护会按隔离文件名恢复对应关系。管理员可在隔离期内用 `/表情 启用 <ID>` 恢复；手动停用会保留原图。

## 备份范围

备份使用 SQLite Backup API 获取一致数据库快照。复制快照、对应原图或隔离文件、当前遗忘账本期间短暂持有数据库写锁，避免与手动恢复交错；配置文件在释放写锁后复制。

| 内容 | 默认备份 |
| --- | --- |
| 主数据库 | 是，保存为备份目录中的 `nonebot.sqlite3` |
| 数据库关联的本地原图或隔离文件 | 是，保留项目相对路径 |
| 当前 `forget-ledger.jsonl` | 是；恢复时仍须使用当前完整账本 |
| `config/nonebot.env`、`config/persona_agent.env`、`config/persona_agent.yaml` | 文件存在时复制 |
| `state/nonebot/active_model.txt` | 文件存在时复制 |
| LLBot WebUI 令牌文件 | 否，需另行保存或重新提供 |
| `PERSONA_AGENT_CONFIG` 指向的其他 YAML 及其同目录密钥 | 不自动包含，需另行保存 |
| 虚拟环境、向量模型缓存 | 否，在目标环境重新生成 |
| LLBot、PMHQ、QQ 客户端及其数据 | 否，由外部环境管理 |

备份目录位于 `backups/nonebot/`，清单 `manifest.json` 的类型为 `nonebot-daily-v1`，记录文件 SHA-256。任务先写 `.incomplete-时间`，全部完成才改为正式目录，不应将未完成目录当作可用备份。

备份包含聊天、记忆与密钥，应保存在私有环境。本机副本无法覆盖磁盘损坏风险，异地副本由部署者按需要另行保存。

## 恢复流程

当前代码使用 schema 5。恢复前核对代码与数据库兼容性，保留故障现场，并将**当前完整遗忘账本及其 SHA-256**单独保存；备份时的旧账本不能覆盖之后发生的遗忘记录。

### 1. 停止写入

停止定时器、可能正在运行的维护服务和主服务：

```bash
systemctl --user stop qqbot-maintenance.timer qqbot-backup.timer
systemctl --user stop qqbot-maintenance.service qqbot-backup.service
systemctl --user stop nonebot.service
```

如果另有前台实例，也应退出。确认所有写入已停止后，再保存当前账本与故障现场。

### 2. 还原数据库、素材与配置

按所选正式备份的 `manifest.json` 核对文件哈希，再还原：

1. 将故障数据库及其 `-wal`、`-shm` 一同移入私有现场副本，避免旧日志文件混入恢复后的快照。
2. 将备份中的 `nonebot.sqlite3` 恢复为 `state/nonebot/persona_agent.sqlite3`。
3. 按清单恢复对应素材，保留 `state/nonebot/stickers/` 与 `cache/quarantine/stickers/` 的相对路径。
4. 恢复所需配置与模型选择，自行补齐 WebUI 令牌或自定义配置文件。
5. 放回单独保存的**当前** `state/nonebot/forget-ledger.jsonl`，不要用旧备份中的版本覆盖它。

迁移到新环境时，在最终安装路径按 `uv.lock` 重新创建 `.venv`，不复用已搬迁虚拟环境中的脚本路径。不得直接覆盖正在运行的数据库。

### 3. 重放当前遗忘账本

```bash
cd "$HOME/bots/qqbot"
nonebot/.venv/bin/python -B ops/reconcile_forgetting.py
```

脚本检查用户级 `nonebot.service` 已停止，打开数据库补齐所需结构并重放账本，不发送聊天消息或调用模型。它不会检查另行启动的前台进程，因此必须先完成停止写入步骤。

账本缺失、损坏或比数据库已应用记录更旧时，停止恢复并寻找当前完整副本。不要删除 applied 表、迁移版本或账本，也不要使用缺少账本检查的旧入口绕过保护。遗忘持久化失败导致模型上下文停用时，也按此离线流程处理。

### 4. 恢复运行

重放成功后启动主服务，确认启动完成，再恢复定时器：

```bash
systemctl --user start nonebot.service
systemctl --user status nonebot.service
tail -n 50 logs/nonebot/nonebot.log
```

```bash
systemctl --user start qqbot-maintenance.timer qqbot-backup.timer
systemctl --user list-timers 'qqbot-*'
```

不要把 `unknown` 发送状态当成未发送而重放。恢复中的当前账本要求与日常遗忘语义见[记忆管理](../docs/memory.md#账本保留与恢复)。
