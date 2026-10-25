# MaiWill 接手与 MaiBot 源码改动核查（2026-10-24）

> 历史快照：以下记录针对修改前的 MaiWill 0.1.13。用户随后明确要求以原版 MaiBot 的可分发插件为边界；0.1.14 已改为插件下拉选择宿主已有任务，并可指定已配置的模型，不再依赖本报告中的宿主补丁。当前用法以 README 和交接文档为准，下面保留核查时的证据与计数。

> 后续处理：0.1.14 的单测、真实 SDK 和原版宿主函数离线验证通过后，已按用户要求备份并清理本文记录的 10 个宿主文件改动。MaiBot-source 工作区恢复干净，HEAD 未变；备份为 `dist/maibot-source-changes-before-cleanup-20261024.zip`。后续以不修改宿主源码为前提，公开接口不足时先与用户讨论。

本次核查基于本机源码、部署元数据、安装包文件校验及离线测试。结论：MaiBot 的本地补丁集中于 `maiwill` 独立模型任务，总计 10 个文件、增加 100 行、删除 5 行，其中生产代码为 5 个文件、增加 30 行、删除 4 行。意志循环、体力、手机、日记、学习候选等业务逻辑仍在 MaiWill 插件内。

首要接手问题是部署版本不一致：开发源码已实现新任务，但一键包当前部署的 MaiBot 1.3.14 尚未包含补丁；已安装的 MaiWill 0.1.13 核心文件与开发目录一致，会请求这个尚不存在的任务。离线调用任务解析函数可复现错误。

## 核查范围和基线

- MaiWill 项目：`C:\MyProgram\MaiWill`，manifest 版本 `0.1.13`。
- MaiBot 源码：`C:\MyProgram\MaiBot-source`，当前 `main` / HEAD 为 `c602a2780`，与本地缓存的 `origin/main` 相同；本次没有刷新远端，不能据此判断远端当前状态。
- 一键包部署目录：`C:\Users\Reshirem\AppData\Roaming\MaiBotOneKeyDesktop\c74f51828bc1\modules\MaiBot`。
- `.onekey-deployment.json` 指向 PyPI 模式、MaiBot `1.3.14`，环境代号为 `eb9243da-7b95-4a08-8889-0d01afdbd4dc`。
- 部署源码位于该目录下 `.onekey/environments/eb9243da-7b95-4a08-8889-0d01afdbd4dc/Lib/site-packages/src`。
- 本报告描述磁盘上的部署环境及离线行为，没有把它等同于正在运行的进程状态。本次没有启动、重启 MaiBot，也没有进行真实模型调用或设备操作。

MaiBot 的这些改动均未提交。MaiWill 仓库尚无首个提交、未配置 remote，项目文件尚未进入 Git 历史，因此无法从 Git 还原 MaiWill 自身各阶段的历史增量；本次以现有实现作为接手快照。

## MaiBot 改了多少

计数以 `git diff HEAD --numstat` 加未跟踪文件的实际行数为准。普通 `git diff --stat` 只显示 9 个文件、增加 86 行、删除 5 行，还需要计入新增的 14 行测试文件。

| 类别 | 文件（相对 MaiBot 源码根目录） | 增加 / 删除 | 实际变化 |
| --- | --- | --- | --- |
| 模型配置 | `src/config/model_configs.py` | +9 / -0 | 增加 `ModelTaskConfig.maiwill` 和模型管理标签 |
| 模型配置 | `src/config/default_model_config.py` | +7 / -0 | 增加空模型列表、16384 输出 token、0.7 温度、random 策略、180 秒超时的任务模板 |
| 模型配置 | `src/config/config.py` | +1 / -1 | 模型配置版本由 `1.17.22` 增至 `1.17.23` |
| 插件能力路由 | `src/plugin_runtime/capabilities/core.py` | +11 / -1 | 显式请求 `maiwill` 且任务存在、模型列表为空、未指定具体模型时，改走 `planner` |
| 模型管理前端 | `dashboard/src/routes/config/model/hooks/useModelConfig.ts` | +2 / -2 | 将 `maiwill` 纳入可选任务，空列表不计入必填任务缺失 |
| 前端测试 | `dashboard/src/routes/config/model/hooks/useModelConfig.test.ts` | +4 / -1 | 覆盖任务显示和可选任务判定 |
| 路由测试 | `pytests/plugin_runtime/test_llm_capability_model_args.py` | +37 / -0 | 覆盖空任务回退及独立模型路由 |
| 配置 Schema 测试 | `pytests/webui/test_config_schema.py` | +9 / -0 | 检查管理标签和回退说明 |
| 默认配置测试（未跟踪） | `pytests/config_test/test_maiwill_model_task.py` | +14 / -0 | 检查新任务默认值 |
| 更新日志 | `changelogs/changelog.md` | +6 / -0 | 记录新增独立模型任务 |
| **合计** | **10 个文件** | **+100 / -5** | **生产代码 5 个文件，测试 4 个文件，日志 1 个文件** |

现有差异没有涉及 Planner/Replyer 的实现、聊天收发主流程、宿主调度器、A_Memorix、手机操作或日记存储。模型路由的修改作用于共享插件能力函数，`llm.generate` 与 `llm.generate_with_tools` 都调用它；当前 MaiWill 实际使用的是 `llm.generate`。

## 对应到 MaiWill 哪些模块

| MaiWill 模块 | 当前入口 | 与宿主补丁的关系 |
| --- | --- | --- |
| 意志模型决策 | `plugin.py:134` 的 `_request_decision`，`plugin.py:276` 的宿主调用 | **直接依赖**新增任务；每轮显式发送 `task_name="maiwill"`，模型由宿主管理 |
| 单一意志会话 | `maiwill/agent.py:19` 的 `AgentLoop` | 决策器失败就不能推进能力往返；会话和动作持久化本身在插件内 |
| 生命周期、休息和断连恢复 | `maiwill/runtime.py:12` 的 `SessionRuntime` | **间接受影响**；模型任务错误会被记录为运行失败并进入持久化退避，默认从 5 秒增加到 600 秒 |
| 体力与用量账本 | `plugin.py:281` 起的 usage 处理、`maiwill/storage.py:848` 起的请求与结算 | 仍按宿主返回的实际 usage 结算；该补丁没有增加 usage 或稳定 provider request ID。缺失 usage 会记为未知 |
| 聊天兴趣记录 | `plugin.py:748` 的 `handle_interest_record`、`Storage.record_interest` | 使用现有 SDK 工具注册及本地 outbox；无需此次宿主改动，意志处理这些事件则依赖模型任务成功 |
| 共享记忆查询 | `plugin.py:325` 的 `_search_memory` | 使用已有 `knowledge.search`，固定显式兴趣来源的 `chat_id` 并启用过滤；此次没有修改记忆查询实现 |
| 学习候选、反思和未来同步 | `plugin.py:378`、`:421`、`:453`，`maiwill/storage.py` | 候选 CRUD 已在插件内实现；正式共享记忆写入尚未接通，此次模型补丁没有实现这一部分 |
| 主动回到聊天 | `plugin.py:492` 的 `_send_proactive_message` | 使用已有 `maisaka.proactive.trigger`；仅允许当前上下文中显式兴趣来源聊天流，宿主主聊天实现未改 |
| 手机操作 | `maiwill/phone.py:53` 的 `AdbPhone`、`plugin.py:613` 的 `_use_phone` | ADB 能力在插件内；此次无手机专用宿主改动，自动操作的决策依赖意志模型 |
| 日记 | `plugin.py:363`、`:582` | 本地 Markdown 写入和幂等命名在插件内；此次未新增宿主日记能力 |
| 配置及状态界面 | `maiwill/config.py`、`webui.json`、`plugin.py:634` | 插件配置管理启停、恢复、体力、手机；意志模型选择在 MaiBot 模型管理页，前端补丁负责允许新任务留空 |

主要调用关系：

```text
MaiWill.on_load
  -> SessionRuntime
  -> AgentLoop
  -> MaiWill._request_decision
  -> ctx.call_capability("llm.generate", task_name="maiwill")
  -> MaiBot 插件 LLM 路由
       -> maiwill 已配置模型：使用独立任务
       -> maiwill 任务存在但列表为空：使用 planner 整套任务配置
  -> 返回决策和 usage
  -> 插件记账、串行执行能力、持久化结果、继续会话或休息
```

## 部署差异与可复现问题

核验当前环境 `maibot-1.3.14.dist-info/RECORD` 中的全部 762 个带校验值的 `src/` 文件，结果全部匹配，没有缺失。对源码仓库与安装环境进行换行归一化后的比较，646 个共同文件相同，只有本报告列出的 4 个 Python 生产文件不同；另有 14 个文档/仓库辅助文件未打入安装包。

安装的 MaiWill 与开发目录逐字节比较了 `plugin.py`、`_manifest.json`、`webui.json` 和 6 个 `maiwill/*.py` 文件，共 9 个文件，全部相同。用户安装配置和运行数据未作为代码进行覆盖或同步。

离线从实际文件提取函数并执行，结果如下：

| 环境 | Schema 含 `maiwill` | 空模型列表的路由结果 |
| --- | --- | --- |
| 开发源码 | 是 | `planner`，任务解析成功 |
| 当前部署的 1.3.14 | 否 | 保留 `maiwill`，随后抛出 `ValueError: 未找到名为 \`maiwill\` 的模型配置` |

这意味着当前宿主虽然满足插件 manifest 声明的版本下限，但自主意志模型链在此任务解析处存在确定的兼容缺口。现有 manifest 仅要求宿主版本至少为 `1.3.14`，该版本号不足以表达“必须包含 MaiWill 模型任务补丁”。“新任务留空时回退”也不等于“旧宿主没有这个任务时回退”。

后续试跑前应先让目标宿主实际包含这项补丁，或明确实现并验证插件的旧宿主兼容策略。当前核查没有选择或实施部署变更。

## 接手时需要纠正或继续推进的边界

1. **共享学习还没有形成写入和召回闭环。** 宿主能力注册表仅有 `knowledge.search`，没有 `knowledge.ingest`。`src/A_memorix/plugin.py` 明确是未由主线加载的 legacy 入口，不能通过插件 `api.call` 绕行。现有兴趣状态是 `pending_host_ingest`，候选的 `resolved` 不代表已经同步。`bot_self` 自我来源也尚未有专用召回策略。相关后续实现与此次模型任务补丁是不同工作范围。
2. **“插件不传输出 token 限制”不等于宿主无限输出。** `src/llm_models/utils_model.py:680` 的 `_resolve_effective_max_tokens` 依次使用调用参数、模型配置、模型额外参数、任务配置。没有其他覆盖时，新任务模板的 `16384` 会生效；回退到 Planner 时使用 Planner 的设置。插件的体力透支语义与输出长度限制需要分开描述。
3. **旧文档关于宿主只能传文本的判断需要更新。** 当前 `core.py` 的 `_normalize_prompt_arg` 支持消息列表，`src/services/llm_service.py:498` 起支持 `image_url` / `image_base64` 图片片段，安装环境中的服务代码也相同。离线解析验证通过。MaiWill 当前仍只发送文本、手机截图只返回本地路径，尚未把图片接入决策请求；真实视觉调用还取决于模型配置和端到端验证，不能把现有缺口直接归结为“必须修改宿主才能支持视觉”。
4. **长会话上下文整理仍需实现评估。** `AgentLoop` 持续追加 `context["messages"]`，`_request_decision` 重发完整上下文。已具备持久恢复，但未看到历史压缩/裁剪路径。长期运行的上下文长度、重复输入成本和模型上下文上限需要单独处理。
5. **建立可追溯的项目基线。** MaiWill 尚无 Git 提交，MaiBot 补丁也未提交。后续开发需要把插件代码、依赖的宿主补丁和部署结果关联起来，避免再次出现代码版本相同却宿主能力不同的情况。

宿主后续修改应遵循现有 `C:\MyProgram\MaiBot-source\AGENTS.md`；已有交接文档记录的授权范围是模型管理任务。A_Memorix 实现改动另有 `src/A_memorix/MODIFICATION_POLICY.md`，本次仅核查，没有实施新的宿主或 A_Memorix 修改。

## 本次验证结果

- 在独立临时副本运行现有 MaiWill 测试；复制的 29 个文件先逐文件核验 SHA-256，确认与项目一致。
- `python -X utf8 -B -m unittest discover -s tests -v`：共 50 项，48 项通过、2 项因环境不支持符号链接而跳过，用时 8.157 秒。测试使用临时数据库、模拟 SDK/能力与 ADB，没有发起真实自主活动。
- 离线执行实际源码的路由函数：空 `maiwill` 回退、已配置独立模型、显式指定具体模型三个场景通过；旧安装环境缺任务的失败已复现。
- 离线验证实际源码的输出长度解析和结构化图文输入解析：均符合上述结论；没有进行模型供应商调用。
- MaiBot `git diff --check` 通过。当前已找到的 Python 环境没有 pytest，本次没有运行 MaiBot pytest 测试；也没有运行宿主前端测试或构建，不能把离线验证等同于完整宿主集成验收。
- 测试日志：`C:\Users\Reshirem\AppData\Local\Temp\maiwill-handoff-audit-l4g5z_t_\unittest.log`，临时目录可能由系统后续清理。

本次新增此核查记录并将其加入交接文档入口，业务代码、宿主补丁及安装环境均未修改。
