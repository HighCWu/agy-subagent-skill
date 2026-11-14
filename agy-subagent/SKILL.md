---
name: agy-subagent
description: Use Antigravity CLI (agy) through Codex-shaped subagent operations and JSON arguments such as spawn_agent, send_message, followup_task, wait_agent, and interrupt_agent. Use when delegating implementation, investigation, review, or a second opinion to agy or Antigravity.
---

# agy 子代理

使用本技能目录内 `scripts/agy_subagent.py` 的绝对路径作为 `AGY_RUNNER`。操作名与 JSON 字段对齐 Codex 当前的 collaboration 接口。通过 shell 调用脚本，JSON 从 stdin、`--args-file PATH` 或 `--json` 读取。长消息和动态文本优先通过 stdin 或 JSON 文件传入。

```bash
python3 "$AGY_RUNNER" spawn_agent <<'JSON'
{"task_name":"investigate_cache","message":"调查缓存失效原因，只读取相关文件，返回路径和证据。","agent_type":"explorer","fork_turns":"all"}
JSON
```

首次使用检查 `agy --help` 和脚本的 `doctor`。需要本机已登录的 agy、Python 3.10+ 和 Linux/macOS。`schemas` 返回各操作的完整 JSON Schema。

## 调用参数

| 操作 | JSON 字段 | 行为 |
| --- | --- | --- |
| `spawn_agent` | `task_name`, `message`, 可选 `agent_type`, `fork_turns`, `model`, `reasoning_effort` | 启动代理，立即返回 `agent_id` 和规范任务路径 `task_name` |
| `send_message` | `target`, `message` | 给运行中的代理排队补充消息，空闲代理只收信 |
| `followup_task` | `target`, `message` | 空闲时启动新轮次，运行中排队追加任务 |
| `interrupt_agent` | `target` | 中断当前轮次，返回 `previous_status` 与当前状态 |
| `list_agents` | 可选 `path_prefix` | 列出当前聊天的代理，可按任务路径筛选 |
| `wait_agent` | 可选 `timeout_ms` | 等待当前代理邮箱中的新消息或结果，默认 30000 毫秒 |
| `close_agent` | `target` | 停止工作并关闭句柄，保留会话与记录 |
| `resume_agent` | `target` | 恢复已关闭句柄，后续用 `followup_task` 分配工作 |

`task_name` 使用小写字母、数字、下划线，同一父代理下唯一。`target` 可使用返回的 ID、相对任务名或 `/root/investigate_cache` 这样的规范路径。嵌套代理的任务路径随父代理扩展，跨分支消息使用规范路径。子代理可通过 `send_message` 给父代理或其他代理发送信息，消息来源和会话由运行环境记录。

`agent_type` 默认 `default`，支持 `worker`、`explorer`、`reviewer`。角色职责会实际注入 agy 提示词。`worker` 要求保留其他人的修改并验证实现，`explorer` 和 `reviewer` 要求使用只读操作。模式与权限由 agy 配置决定，角色要求属于提示词约束。

`model` 传给 agy 的 `--model`，使用 `agy models` 中的模型 ID。`reasoning_effort` 传给 `--effort`，支持 `low`、`medium`、`high`、`xhigh`、`max`。`ultra` 映射为 agy 的 `max`，返回值会报告实际 `agy_effort`。省略时沿用 agy 配置，嵌套代理沿用父 agy 代理的设置。

## 上下文和工作区

`fork_turns` 默认 `all`。在本地 Codex 中，脚本根据当前聊天 ID 读取本聊天已落盘的用户与助手文本，并作为父上下文传入。嵌套 agy 代理读取其父代理的任务与回答。传入 `none` 可创建独立任务。

当前环境缺少可读聊天记录时，使用扩展参数 `parent_context` 或 `context_file` 提供上下文，或按任务需要明确选择 `none`。返回的 `context_source` 说明上下文来源。系统指令、隐藏推理、工具配置、权限和审批设置需要按 agy 的执行环境处理。把任务所需的项目约束、文件所有权、目标和验收条件写入 `message`。

工作目录默认使用调用位置，嵌套代理继承父工作目录，也可通过扩展字段 `cwd` 指定。独立子任务可并行 `spawn_agent`。协作编辑时在 `message` 中分配文件责任，并检查合并后的实际差异。

agy 特有选项放入可选 `agy` 对象。

```json
{
  "task_name": "fix_cache",
  "message": "修复 src/cache.py 中的失效问题，保留其他人的修改并执行现有测试。",
  "agent_type": "worker",
  "fork_turns": "none",
  "cwd": "/absolute/project",
  "model": "an-id-from-agy-models",
  "reasoning_effort": "high",
  "agy": {"mode": "accept-edits", "timeout": 900}
}
```

`agy` 支持 `bin`、`mode`、`agent`、`project`、`add_dir`、`sandbox`、`skip_permissions`、`timeout`。新接口默认沿用 agy 模式，并等待任务完成。`timeout` 可显式设置每轮秒数。`skip_permissions` 映射到 `--dangerously-skip-permissions`，启用后自动批准 agy 工具请求，按当前任务授权和执行环境选择。

## 消息与等待

```bash
python3 "$AGY_RUNNER" send_message <<'JSON'
{"target":"investigate_cache","message":"补充检查并发写入场景。"}
JSON
python3 "$AGY_RUNNER" followup_task <<'JSON'
{"target":"investigate_cache","message":"依据调查结果实现修复，修改范围为 src/cache.py。"}
JSON
python3 "$AGY_RUNNER" wait_agent <<'JSON'
{"timeout_ms":30000}
JSON
```

运行中消息由持久队列保存，在当前 agy print 轮次结束后自动投递到同一 conversation 的下一轮。需要立即改变方向时，调用 `interrupt_agent` 后使用 `followup_task`。首次初始化前中断导致 conversation ID 尚未生成时，续接会携带原任务和补充消息启动。

`wait_agent` 等待当前会话中发给调用方的任一新消息，返回 `updates`，超时返回 `timed_out=true`。每个代理拥有独立邮箱游标，已读取事件会被消费。`timeout_ms=0` 可立即收取已到达的更新。

结果事件包含 `sender`、`agent_id`、`turn`、`status`、`message`、`error` 和 `more_work`。`more_work=true` 表示该结果后还有已启动的排队工作。使用 `list_agents` 确认相关代理均完成，并核查实际文件和验证结果后整合交付。父代理负责持续监控用户要求完成的所有工作。

会话默认根据 `CODEX_THREAD_ID` 隔离，agy 子代理自动继承所属会话。脱离 Codex 时，可设置固定 `AGY_SUBAGENT_SESSION` 以便跨目录访问同一组代理。

状态与日志默认保存在 `~/.local/state/agy-subagent/`。顶层参数 `--state-dir` 或环境变量 `AGY_SUBAGENT_STATE_DIR` 可更改位置。诊断时可调用旧接口 `result AGENT_ID` 查看本轮结构化结果与日志目录。关闭和中断保留已有修改及记录，重试前核查副作用。

接口差异、状态恢复和旧版命令说明见 [references/behavior.md](references/behavior.md)。
