# MaiBot 接入边界

本文档记录原版 MaiBot 的公开接口与未支持能力。MaiWill 必须可以作为插件分发，安装不要求修改宿主源码；本地 MaiBot-source 仅作实现核查参考，正式兼容性以未打补丁的发行环境为准。0.1.14 起不依赖此前本地添加的 maiwill 模型任务。

## 已有能力

当前插件运行时可以使用这些宿主能力：

| 能力 | 用途 |
| --- | --- |
| `llm.generate` | 普通一次模型调用 |
| `llm.generate_with_tools` | 让模型返回结构化工具调用 |
| 现有模型任务与 model_name | 插件下拉选择宿主已有生成任务，也可仅为当前请求指定宿主已配置的模型 |
| `tool.get_definitions` | 获取 MaiBot 当前可用工具定义 |
| `chat.get_*`、`chat.open_session` | 解析和打开聊天流 |
| `maisaka.proactive.trigger` | 将主动意图交给 MaiBot 正常处理 |
| `send.text`、`send.image` | 由插件直接发送已经决定的内容 |
| `knowledge.search` | 查询 A_Memorix 长期记忆 |
| `api.call` | 按插件 ID、API 名称和版本调用公开的插件 API |
| `config.get`、`config.get_plugin` | 读取宿主和插件配置 |

MaiWill 应优先通过这些能力完成共享记忆查询和主动交互，不直接导入 MaiBot 的内部模块。所有自主能力和模型工具往返归属于 MaiWill 的同一个逻辑意志会话，不要求插件读取普通聊天消息；聊天兴趣由正常聊天模型显式写入后作为事件投递给该会话。

`chat.proactive` 已在插件侧适配 `maisaka.proactive.trigger`。为限定第一版的目标范围，仅允许意志会话把主动表达发往当前逻辑会话上下文中包含显式兴趣记录事件的聊天流；MaiBot 的正常回复流程接管后续处理。

用户在 MaiWill 插件设置的 general.model_task 下拉框选择 planner、replyer、utils、memory、mid_memory、learner、fast_model、emoji 或 vlm，默认 planner。插件将选中值传给 llm.generate 的 task_name。general.model_name 留空时使用任务的模型列表和策略，填写时通过已有 model_name 参数为当前请求指定具体模型，其余任务参数仍沿用所选任务。宿主已有继用规则照常生效，插件不把无效模型或任务静默替换为其他任务。模型与凭据由宿主管理，意志运行器、事件、工具执行、状态与恢复由插件管理。

MaiWill 从自主 LLM 能力调用结果读取 prompt、completion 和 total token 用量。体力结算以宿主返回的实际 usage 为准，不按 LLM 自报或提示词长度估算。若响应缺少完整 usage，MaiWill 保留用量未知标记并告诉决策模型真实成本可能更高，不将缺失数据视为零，也不阻止后续调用。

llm.generate 和 llm.generate_with_tools 返回 usage 分项，但没有稳定的 provider request ID。插件为每次请求生成本地幂等 ID；缺少 usage 或重启未结算时记录 unknown，不伪报零消耗。插件不额外传入 max_tokens，宿主模型/任务限制仍生效。宿主已接受结构化消息列表和图片片段；MaiWill 现将自身截图目录内最近一次有效截图作为临时图片片段交给 llm.generate，不将 Base64 存入持久上下文。

MaiWill 不统计普通 Planner/Replyer 聊天的 token，因此无需读取聊天正文或增加聊天用量 Hook。现有麦生活的插件调用统计可作为实际 usage 字段与聚合展示的参考。

## 当前接入发现

MaiWill 已有插件侧 SQLite 账本、兴趣 outbox、状态 API、WebUI 和本地学习候选。正式插件能力仍没有 A_Memorix 写入和 bot-self 跨聊天召回 API。0.1.32 为个人使用增加默认关闭的本机 WebUI 桥接，使用管理 WebUI Token 调用粘贴导入、记录搜索和段落详情接口；这不是 MaiBot 公开的插件 API，也不适合作为通用分发权限模型。源码还显示 `knowledge.search` 通过请求中的 `chat_id` 建立范围；`chat_id` 为空时不建立范围，不能安全用于私人插件发起的跨聊天“自我记忆”检索。插件现已限制 `memory.search` 只能用于当前逻辑上下文中显式记录兴趣的来源流，并把该流 ID 固定传给宿主；没有兴趣来源时不开放检索。

### 个人用途 WebUI 记忆桥接

在 MaiWill 插件配置的“意志会话”中显式开启 `memory_bridge_enabled` 后，插件会从 `MAIBOT_INSTANCE_DIR` 指向的 MaiBot 实例 `data/webui.json` 读取 `access_token`，作为 `maibot_session` Cookie 向 `memory_webui_url` 发请求。地址必须是 localhost 或回环 IP；代理关闭、重定向拒绝，Token 不写入 MaiWill 配置、数据库或错误消息。该 Token 具有 WebUI 管理权限，因此启用桥接就等于授权 MaiWill 以管理员身份操作这台本机 MaiBot；要撤销授权，可关闭开关或轮换 WebUI Token。

读取端只返回来源符合 `web_import:maiwill-<稳定摘要>.json` 且正文包含同一稳定候选 ID 标记的段落。写入端只接受本地状态为 `resolved` 的学习候选，内容固定来自 SQLite 中保存的标题、正文和标签；WebUI 页面确认后提交至 `/api/webui/memory/import/paste`，关闭 LLM 抽取，设为全局范围。成功后轮询导入任务，再检查来源文件名和正文标记以确认写入。WebUI 写入属于可持久化操作，完成后可以通过 MaiBot 记忆管理删除。

这是供作者自用的短期适配：WebUI 路由、Cookie 认证、实例目录发现或导入语义改变时可能失效。默认关闭；必须设置 `MAIBOT_INSTANCE_DIR` 指向正确实例，否则失败关闭；不会在单元测试中读取真实 Token 或连接真实实例。正式公开的细粒度插件 API 发布后应替换此桥接。

### 1. 正式插件边界目前没有共享记忆写入能力

MaiBot 能力注册表只公开 `knowledge.search`，没有通用 `knowledge.ingest`。虽然 `src/A_memorix/plugin.py` 保留 `ingest_text` API 及其稳定 `external_id` 写入实现，但文件顶部说明该模块是 legacy compatibility entry，MaiBot 主线不再发现或加载它。当前实际接入位于内部 `src/A_memorix/host_service.py`，不属于 MaiWill 可调用的插件 API。`api.call` 只能调用已注册并公开的插件 API，因此不是可用绕行方式。

以下是未来公开能力的契约草案，不是当前插件依赖，也不是用户需要修改源码的安装步骤。只有正式宿主或公开插件 API 提供受控写入能力后才接入，例如：

```text
knowledge.ingest
```

它应支持稳定 `external_id`、`source_type`、正文、时间、标签、元数据、人物或聊天范围和 `respect_filter` 等字段，并返回幂等写入结果。

建议的插件调用契约是 `knowledge.ingest`，仅接收 MaiWill 候选需要的字段：`external_id`、允许列表中的 `source_type`（`mai_will.knowledge/experience/expression/phone/diary`）、`text`、`timestamp`、`tags`、`metadata`。宿主应从受信能力调用方/插件身份推断并强制 `owner_scope=bot_self`、`scope_type=global`，丢弃或拒绝调用方尝试传入的 `chat_id`、`person_ids`、`user_id` 和 `group_id`，避免自主知识误归到用户聊天。写入仍应调用 A_Memorix 现有 `ingest_text`，保留 `external_id` 幂等检查，并返回 `stored_ids`、`skipped_ids`、`reason`；若相同 ID 指向不同正文，必须返回冲突，而不能当作 `exists` 成功。过滤策略对 bot-self 导入应按独立宿主策略决定，不以空 `chat_id` 让通用聊天过滤隐式放行。

### 2. `bot_self` 召回仍是实现缺口

当前 A_Memorix 段落范围解析读取 `scope_type=global`、聊天 ID 或特定导入来源；未标明范围且非 `web_import:` 来源的段落会被归为 unknown。单纯在 metadata 中加入 `owner_scope=bot_self` 不够：当前 `_paragraph_scope_identity` 不识别这个字段，且需确认 `knowledge_type` 过滤与普通聊天提示构造允许这些条目。建议将 `bot_self` 实现为上游 `MaiBot_branch` 中的显式受信来源策略：仅对由 `mai_will.*` 来源写入且 `owner_scope=bot_self` 的段落授予跨聊天可见性，不把所有 `global` 或未知范围段落开放给聊天；保持人物/聊天来源范围过滤不变，并限制每轮返回数量、避免私人聊天正文进入该范围。插件传 `chat_id` 检索当前也会按聊天范围筛选而排除没有该聊天 ID 的 bot-self 条目，因此跨聊天自我召回应由 A_Memorix 正常聊天召回路径实现，而不是让插件无范围检索绕过过滤。

跨聊天自我来源召回需由正式宿主公开支持，并保留用户/聊天来源的过滤。当前插件不修改 MaiBot 或 A_Memorix 来补齐该策略。

### 3. 工具定义查询不等于工具执行

`tool.get_definitions` 可以返回 MaiBot 工具定义，`llm.generate_with_tools` 可以返回模型工具调用，但当前插件能力没有一个“按工具名执行任意 MaiBot 工具”的通用入口。

第一版不应把整套 MaiBot 工具直接交给后台意志。MaiWill 先实现自己的能力适配器，并对需要宿主执行的功能使用明确的能力接口；后续若确有需要，再增加带权限、超时和结果规范的工具执行能力。

### 4. 聊天提示词本身不能持久化兴趣

给聊天模型增加“记录感兴趣内容”的提示，只能改变模型在本轮的判断。要让兴趣跨会话存在，聊天工具执行链必须实际调用 A_Memorix 写入，或调用 MaiWill 的 `interest.record` 并保存到可靠 outbox。后台不需要另外读取聊天记录来补做兴趣识别。

## 未来公开能力的契约草案

默认关闭的个人 WebUI 方案已接通已整理候选的手动写入和 MaiWill 来源读回。聊天兴趣 outbox 自动同步、受控插件写入 API 与普通聊天的 `bot_self` 跨聊天召回仍未接通。以下保留为未来公开能力的评估标准，不属于当前桥接依赖：

1. 在 MaiBot 接入层注册并授权 `knowledge.ingest`，把 MaiWill 自我记忆参数规范化为内部 `ingest_text` 请求，并在 host service 内以 `owner_scope=bot_self`、`scope_type=global` 覆盖元数据后赋予可信范围；不可信任插件调用方自行声明的 scope 字段。
2. 在 A_Memorix 上游 `MaiBot_branch` 扩展正常记忆召回，使受信 `owner_scope=bot_self` 的记录可跨聊天流作为内部背景；明确过滤类型、提示词和数量策略，并保证私人用户事实绝不进入该范围。
3. 保留来源、人物、聊天流和幂等元数据，避免把 MaiWill 的内容混成普通聊天摘要。
4. 让 `interest.record` 的 outbox 可经可靠的同步任务进入同一宿主写入接口；为写入重放、冲突、过滤和普通聊天召回添加接入测试，确保旧聊天记忆和人物范围不回归。
5. 公开模型接口若增加稳定请求 ID，可用于恢复实际 usage；当前插件继续保留未知用量记录，普通聊天不进入体力账本。

正式宿主写入 API 出现后，建议至少保证：相同 `external_id` + 相同正文返回幂等成功；相同 ID + 不同正文明确报冲突；成功响应包含 `stored_ids` 或可识别的 `already_exists`，让 outbox 能安全重试；过滤跳过不得被误报成存储成功。MaiWill 已为学习候选实现独立同步状态、尝试次数和错误，不把 `resolved` 混作 `synced`。

不建议在 MaiBot 核心中增加 MaiWill 专用调度器或手机逻辑；这些属于插件能力和同一个意志会话。

## 插件开发顺序

用户于 2026-10-24 明确要求不修改宿主源码。插件不运行时修补宿主、不导入内部 host_service，不以私有补丁作为安装前提。

模型选择已经改用原版公开能力；后续继续在插件边界内开发：

1. 继续保留 MaiWill 本地候选队列；个人 WebUI 桥接开启后才允许显式同步，正式插件 API 出现后替换临时适配器。已同步候选保留本地关联记录；删除共享副本需在 MaiBot 记忆管理中操作。
2. 优先完成插件内可独立交付的模型选择、设备适配、日记、候选管理和会话恢复。
3. 正式宿主或公开插件 API 提供写入能力后，再核实版本、权限、来源范围、幂等和召回契约，添加可选适配与集成测试。

公开接口尚不支持的能力明确保持未接通，现有插件功能可以独立运行并保留本地学习结果。


