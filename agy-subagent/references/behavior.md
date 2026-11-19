# 接口与运行边界

## 参数协议

主要接口对齐 Codex desktop 当前暴露的 collaboration 操作。

```json
{"task_name":"cache_review","message":"Review src/cache.py","agent_type":"reviewer","fork_turns":"all","model":"agy-model-id","reasoning_effort":"high"}
```

`spawn_agent` 额外支持 `cwd`、`parent_context`、`context_file` 和 `agy`。这些扩展处理外部 CLI 的工作区、上下文和提供方选项。脚本的 `schemas` 命令给出可机器读取的完整参数定义，未知参数会报错，避免拼写错误被静默忽略。

`fork_turns=all` 使用当前 Codex 聊天已落盘的用户和助手文本，或显式提供的上下文。它读取当前 ID 对应的 JSONL 文件，排除内部推理、工具结果和运行时指令。这个功能依赖本地聊天记录格式。`context_source` 会报告 `codex-chat-messages`、`parent-agent-history`、`provided`、`file` 或 `none`。环境缺少上下文时会给出可操作的错误。`fork_turns=none` 省略父上下文。

`agent_type` 为提示词角色，`agy.agent` 对应 agy 自身的命名 agent。新接口沿用 agy 执行模式，显式的 `agy.mode` 覆盖它。顶层 Codex 模型与 agy 模型属于各自的提供方配置，`model` 应传入 agy 可用 ID。`reasoning_effort=ultra` 映射为 `--effort max`，返回值包含输入强度和实际 `agy_effort`。

## 生命周期和并发

每个代理有持久任务状态，每轮有独立目录、request 和 result。PTY 终端日志保存在任务目录，旧 print 通道另有 events.ndjson 与 stderr.log。`spawn_agent` 创建任务，后续轮次用精确 conversation ID 续接。首次初始化前取消时缺少 ID，下一次分配会带上已知上下文启动。`close_agent` 关闭句柄并保留结果，`resume_agent` 恢复终止状态，再由 `followup_task` 启动执行。

运行中的 `send_message` 与 `followup_task` 都写入队列。supervisor 在一轮完成时，持有任务锁读取队列，合并待投递消息并启动下一轮。中断、关闭和队列转移在同一任务锁下协调。取消标记会阻止本轮自动继续排队工作。空闲时 `send_message` 仅收信，`followup_task` 会把已有消息一起投递并启动下一轮。

本实现默认使用持久化 PTY 交互模式。运行中投递发生在完整轮次边界。即时改向可使用中断和追加任务组合。Codex 原生运行时拥有采样与工具边界消息路由、原生 UI 展示和父权限继承，外部 agy 的相关行为由其进程和配置控制。

每个 Codex 聊天拥有独立任务命名空间和邮箱。句柄 ID、相对任务名、规范路径均在当前聊天中解析。嵌套代理通过环境变量继承命名空间与自身规范路径。代理间消息由队列或邮箱保存，父代理与子代理分别消费各自收件游标。

`wait_agent` 等待任一发给调用方的新消息。每次返回会消费本次事件，调用方应保留结果并通过 `list_agents` 核实当前状态。单轮完成事件中的 `more_work=true` 表示队列已启动后续工作。

## PTY 与只读结果数据库

Python 标准库创建 PTY，由独立后台控制器持有主端，交互式 agy 的 stdin/stdout/stderr 均连接从端。控制器通过私有目录内 Unix socket 接收请求，回填终端颜色和光标查询，通过 bracketed paste 输入多行消息。无需图形显示和终端模拟器进程。当前实测 Linux、agy 1.5.12；macOS 的进程组和 PTY 行为尚未实测。

每轮加入不可复用的 `[agy-subagent-turn UUID]` 标记。新会话先按精确工作区 URI 筛选数据库，再匹配输入记录中的本轮标记；已知 ID 后只读取该会话。完成条件为本轮后最终 assistant 步骤已完成、无未完成步骤、summary idle 且回答短暂稳定。不会采用不属于本轮的旧回答。读取仅使用 SQLite `mode=ro` 和 `query_only`，每次读取后关闭连接，不修改用户数据库。

适配器读取 `conversation_summaries.db` 与 `conversations/UUID.db`。当前结构为 `steps.step_type=14` 用户输入、`15` assistant 回答，完成状态 `3`，`step_format=0` 的 Step protobuf 第 20 字段内第 1 字段为公开回答。内部推理字段不作为结果读取。schema 不兼容、同会话出现额外用户输入或标记歧义时失败，并给出终端诊断入口。内部格式并非稳定 API，需随 agy 更新维护。

`terminal_status` 返回 PID、ready、active_run_id、conversation_id、needs_input、screen 和日志路径。screen 是去除主要 ANSI 序列后的输出尾部，不是完整终端屏幕重建；`needs_input=workspace_trust` 仅识别已知工作区提示，其他确认需检查 screen。`terminal_input` 只允许文本或枚举按键，用于实际提示交互，不能同时传入 text 和 key。

每轮完成后保持 agy 进程。取消先发送 Ctrl-C，再结束自有进程组，避免工具子进程残留；恢复时通过 `--conversation` 重新启动。rewind 清空当前 conversation ID，下一轮重新启动新会话并重建上下文。宿主必须允许后台控制器存活。状态、终端日志和数据库均包含任务文本；新建状态目录与 socket 目录权限为 0700。

原 print 通道默认禁用。`configure_backend {"backend":"print"}` 显式恢复，`{"backend":"pty"}` 再次禁用；配置写入状态根目录 `backend.json`，仅影响新启动轮次。无自动降级。`AGY_SUBAGENT_RESTORE_PRINT=1` 为测试和明确恢复提供等价覆盖。切换到 print 的下一轮会清理该代理原有 PTY。交互模式不代表服务条款或封控政策允许第三方编排。

## 旧 print 协议（默认禁用）

脚本使用以下实测接口。

```json
{"event":"init","conversation_id":"conversation-id","init":{}}
{"event":"step_update","step_update":{"state":"ACTIVE","text_delta":"..."}}
{"event":"result","result":{"conversation_id":"conversation-id","status":"SUCCESS","response":"answer","duration_seconds":3.7,"num_turns":1,"usage":{}}}
```

新接口把任务和父上下文写入输入文件，再以 `--print "" --input-format stream-json` 通过 stdin 传入，输入格式如下。

```json
{"event":"user","message":{"role":"user","content":"task and parent context"}}
```

文件输入支持长聊天上下文，也让 supervisor 可以在 agy 启动读取时继续处理取消和超时。stdout 为逐行 JSON，全部保存在 `events.ndjson`。init 出现后保存 conversation ID，result 出现后判断轮次状态。成功需要 `status=SUCCESS` 和非空文本 response，CLI 原始错误保存在 error 中。未知事件会被保留并跳过。

脚本向 agy 传入 `--print-timeout 0`。新接口默认等待完成，显式 `agy.timeout` 由 supervisor 执行。接收结果后清理剩余 CLI 进程，避免退出清理阻塞任务。supervisor 和 agy 各自创建独立进程会话，取消时先发送 SIGTERM，最多等待三秒后发送 SIGKILL。

运行环境需允许后台子进程存活。宿主回收进程后，读取状态和实际工作目录以确认执行结果。状态目录含提示词、事件和回答，新建目录权限为 0700，已有目录保留原权限。

## 旧接口兼容

原有 `spawn`、`status`、`list`、`wait`、`result`、`send`、`interrupt` 和 `close` 保留原参数与记录兼容性，执行通道统一受 backend 设置控制。已有状态目录仍可读取。旧 `spawn` 的角色模式、600 秒超时和旧 `send` 的空闲要求继续适用于旧接口。新任务统一使用 Codex 风格操作，以获得邮箱、消息排队、会话隔离和恢复语义。

## 来源

- [Codex Subagents 官方文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)，核实角色、编排、上下文和权限行为。
- 当前 Codex 会话暴露的 collaboration 工具定义，核对操作名与字段。
- 本机 `agy --help`、`agy models` 与真实 print/续接运行，核实参数及事件。
- [sysCat64/codex-agy-plugin](https://github.com/sysCat64/codex-agy-plugin)，参考本机 print 模式集成思路。脚本独立实现。
