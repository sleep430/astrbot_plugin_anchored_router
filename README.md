# astrbot_plugin_anchored_router

把 dsh-anchored-standard 的"锚定首轮"效果迁移到 AstrBot：为绑定会话注入 dsh 极简模式（Minimal condition）的首轮环境作为 warm-up。对插件用户来说这是首轮会话；对模型来说，用户的真实首条消息已经是**第二轮**——模型只能从历史推断"自己"在极简工具面下完成过一轮任务，从而延续该轨迹风格。

## 注入内容（三部分，分别处理）

1. **system**：把 dsh 极简 system prompt（`You are a helpful software engineer assistant.`，与 dsh wire 逐字节一致）前置到 `req.system_prompt` 最前方，并写入 `# Persona Instructions` 区域。每轮幂等执行（AstrBot 每轮重新组装 system prompt）。AstrBot 自身的 Safe Mode / persona / skills / 工具规则原样保留在其后。
2. **虚拟首轮**：`seed_round1.json`（真实录制的 dsh 极简首轮：用户任务 → 带推理的 `bash` 工具调用 → 工具返回 → 带推理的收尾回复），剪裁为 AstrBot 原生消息形态（user/assistant 的 content 均为 blocks 列表，think 块由 AstrBot 在发送时还原为 `reasoning_content`），插到 `req.contexts` 最前端，随历史持久化。
3. **工具变更提醒**：仅在注入轮，向当前用户消息追加一个 `<system_reminder>` 文本块（AstrBot 原生形态，与 datetime 提醒并列）：说明可用工具集已更新、以当前请求携带的工具列表为准。不提及任何先前环境。

## 工具策略（tool_strategy 配置）

- `reminder`（默认）：不注册额外工具。历史中的 `bash` 调用相对当前工具列表是孤儿，由第 3 部分的提醒说明工具集已变更。
- `register`：额外把 dsh 极简版 `bash` 与 `str_replace_editor` 注册进绑定会话每次请求的 `tools` 字段——schema 逐字保真自 dsh wire（见 `dsh_tools.json`），且为**真实可执行实现**（bash 经系统 bash/sh 执行，120s 超时、30k 字符截断；editor 支持 view/create/str_replace/insert）。历史调用不再是孤儿，模型在实际首轮及后续均可真实调用这两个工具。
- `minimal`：在 register 基础上移除其他全部工具（含 `send_message_to_user`、Computer Use 的 `astrbot_*` 系列），使 `tools` 字段恰为 dsh 极简两件套；同时清理 system prompt 中点名被移除工具的段落（`## Skills` 块、Current workspace 段），否则模型会对不存在的工具输出明文 `<｜｜DSML｜｜tool_calls>` 并直接流给用户。此策略下即使 AstrBot 侧完全关闭工具，也会兜底创建仅含两件套的工具集。

### 1.1.2 新增策略（不注入首轮虚拟对话，仅操作 system 字段与 tools 列表）

两策略共用行为：minimal 面下把 system prompt 中点名原版工具的段落**改写**为 dsh 两件套版本（而非删除留空）——`## Skills` 块仍移除；Current workspace 段保留路径、工具名替换为 `bash`/`str_replace_editor`；本地环境段中托管 shell session 的句子移除（dsh `bash` 为一次性命令执行，无 session 概念）。同时两个 dsh 工具的默认工作目录对齐会话 workspace（而非 AstrBot 进程 cwd）。

- `minimal_lean`（1.1.2a）：只做第 1 部分 system 注入，并使绑定会话**永久**保持仅 `bash` + `str_replace_editor` 两件套状态。无 seed、无 reminder。
- `minimal_promote`（1.1.2b）：在 minimal_lean 基础上加入类似 dsh anchored 的晋升机制——模型首次调用工具后，**loop 内下一轮 LLM 请求**即恢复全量 AstrBot 工具（通过 `on_llm_tool_respond` 钩子把备份的全量工具加回 runner 复用的 ToolSet，dsh 两件套保留），工具更新 `<system_reminder>` 追加在触发晋升的那条工具结果尾部（dsh cot-drip 同款落点）；此后该会话保持全量工具 + dsh 两件套（仍保留每轮 system 行注入）。

配置方式：WebUI 插件配置页，或 `data/config/astrbot_plugin_anchored_router_config.json` 中写 `{"tool_strategy": "minimal"}`（插件每请求重读该文件，改后无需重载）。

## 研究报告

完整实验、抓包与路由分析见：[deepseek-v4-pro-routing-research](https://github.com/sleep430/deepseek-v4-pro-routing-research)。

## 设计依据

- dsh 的 DeepSeek V4 模型对 API 可见的工具目录强条件依赖：Minimal 条件产出 "We need…" 轨迹；首轮注入（AGENTS.md 摘要、技能目录）会破坏锚定（dsh-anchored-standard issue #6/#11）。
- AstrBot 启用工具时无法禁用硬编码的 `send_message_to_user` 等工具，因此"清空首轮工具"（见同仓库旧插件 `astrbot_plugin_first_round_no_tools`）不可行；改为注入合成首轮历史（phase ABCD → A'）。
- 经抓包实证（`format-research/FORMAT_REPORT.md`）：dsh 与 AstrBot 均以 OpenAI chat-completions JSON 通讯，工具描述只走 `tools` 字段、由模型服务端模板渲染到 system 区域；"AstrBot 每轮裁掉旧工具描述"的猜测不成立。种子按 AstrBot 线上形态重剪，保证注入物与真实历史逐字节同构。
- 种子首轮取自真实录制（deepseek-v4-pro，anchored-standard 预设 bootstrap 阶段，首轮 header 恰为 `["bash","str_replace_editor"]`）。

## 安装

把整个目录放到 `AstrBot/data/plugins/astrbot_plugin_anchored_router/`，重启 AstrBot 或在 WebUI 重载插件。

## 指令（管理员）

- `/anchor_here`：绑定当前聊天会话（按 `unified_msg_origin`）；本会话每个对话的首轮前注入 warm-up。
- `/anchor_conv`：绑定当前数据库对话（UMO + conversation cid），`/new` 后不自动延续。
- `/anchor_umo <umo>`：手工绑定指定 unified_msg_origin。
- `/anchor_off` / `/anchor_off all`：解绑当前会话 / 清空全部绑定（不回滚已写入历史的种子）。
- `/anchor_status`：查看状态。

## 行为说明

- 注入位置：`req.contexts` 最前端（persona begin_dialogs 之前）。消息**不**标 `_no_save`，随本轮正常写入对话历史；后续请求通过种子里的 `call_anchor_seed_` 工具调用 id 前缀识别，不重复注入。对话被 `/new` 重置或历史截断裁掉种子后，下次请求会自愈式重新注入。
- system 注入（第 1 部分）每轮执行且幂等：只在前方/persona 区缺少该行时补写。
- `register`/`minimal` 模式下两个 dsh 工具经 `req.func_tool` 按请求注入，不影响未绑定会话，也不写入全局工具注册表；`req.func_tool` 为 None（AstrBot 侧完全关闭工具）时兜底新建 ToolSet。
- 状态保存在 `data/plugin_data/astrbot_plugin_anchored_router/state.json`；`minimal_promote` 的晋升标记（`mode=full`）也在其中，解绑 `/anchor_off` 会一并清除。

## 种子内容（seed_round1.json）

录制任务：读取 workspace 根的中性 AGENTS.md（内容为"此 workspace 仅用于建立轨迹、无项目"等温和行为先验），用一次 `bash cat` 完成，收尾 "Instructions loaded."。满足：有真实推理链（"We need respond…"）、有工具调用、一次调用完成、内容自包含不污染后续任务。

## 重新录制种子

`seed-pipeline/`（仓库根目录）内有完整管线：

```bash
# 安装 dsh（一次性）：mkdir _refs/dsh-run && cd _refs/dsh-run && npm i @deepseek-ai/dsh
# 安装 anchored-standard 预设（一次性）：cp -R <dsh-anchored-standard>/preset ~/.dsh/.agent-presets/anchored-standard
cd seed-pipeline
node record.mjs --preset anchored-standard --task "<任务文本>"   # 需 DEEPSEEK_API_KEY 或 ~/.dsh 凭证
node trim_convert.mjs <session.jsonl.zstd> --turn 1 --out seed_new.json   # 支持 zstd 多帧
```

挑选轨迹后把消息里的工具调用 id 改写为 `call_anchor_seed_` 前缀、把 user 消息剪裁为 AstrBot blocks 形态，替换插件目录下的 `seed_round1.json`。

## 注意

- 与旧策略（清空首轮工具）互斥，不要与同仓库的 `astrbot_plugin_first_round_no_tools` 同时启用。
- 合成首轮会出现在 AstrBot WebUI 的对话历史里（这是"写入历史"方案的可见代价）。
- `register`/`minimal` 模式的 `bash` 工具会真实执行 shell 命令（**无沙箱**）：Windows 主机依赖 Git Bash（`bash.exe` 在 PATH 中），Linux/macOS 用系统 shell。请自行评估部署环境的接受度；不需要真实执行时保持默认 `reminder` 即可。
- 模型按 dsh 训练惯例偏好绝对路径 `/workspace/...`：Windows 上会落到当前盘符根目录（如 `D:\workspace\`），Docker（root）内会在容器根创建 `/workspace`。测试后注意清理；生产部署建议在容器内运行。
- 注入词为良性任务记录与中性环境说明，不含任何攻击性内容。
