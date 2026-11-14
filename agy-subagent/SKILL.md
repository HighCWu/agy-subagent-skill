---
name: agy-subagent
description: Delegate scoped tasks to Antigravity CLI (agy) and manage them as background agents with task handles, parallel execution, status, results, conversation follow-ups, interruption, and closure. Use when the user requests agy or Antigravity delegation, implementation, investigation, review, or a second opinion.
---

# agy 子代理

使用本机已安装并登录的 Antigravity CLI 完成明确的子任务。父代理负责分工、监控、核实结果和最终交付。

## 准备和分工

将本技能目录下 `scripts/agy_subagent.py` 的绝对路径记为 `AGY_RUNNER`。通过 `python3 "$AGY_RUNNER" --help` 查看命令，首次使用运行 `doctor`，并检查 `agy --help`。脚本需要 Python 3.10+ 和 POSIX 系统（Linux/macOS）。`doctor` 只检查程序是否存在，首次实际请求会验证登录状态。

根据任务选择 `default`、`explorer`、`reviewer` 或 `worker`。角色是分工标签，其中 `worker` 默认使用 agy 的 `accept-edits` 模式，其他角色默认使用 `plan` 模式。可用 `--mode` 明确指定。权限与模式的区别见 [references/behavior.md](references/behavior.md)。

提示词应包含以下信息，按实际需要取舍。

- 子任务目标、输入文件、必要背景、验收条件和输出要求。
- 当前工作目录、允许修改的文件或模块、相关项目规则。
- 其他代理正在共同工作时，要求保留他们的修改，并协调接口。
- 调查或评审任务明确要求仅返回结论，避免写操作。实现任务写明编辑范围和验证方法。

父代理的上下文和工具权限需要显式传递。将必要背景写入提示词，避免依赖 agy 自动获得聊天历史。AGENTS.md 中适用的约束也要传入。使用文件传递长提示词，避免 shell 插值。按任务所需传递数据。

## 启动与并行

```bash
python3 "$AGY_RUNNER" spawn --cwd /absolute/project \
  --name investigate-cache --role explorer \
  --prompt-file /absolute/task-prompt.txt
```

返回 JSON 中的 `id` 是本地任务句柄。保留它，用于后续所有操作。不同任务可独立 `spawn` 并行运行。并行编辑应分配独立文件或使用已有的隔离工作目录，合并后核实差异。

可选参数包括 `--timeout`（每轮秒数，默认 600）、`--add-dir`（可重复）、`--project`、`--agent`、`--sandbox`、`--model`、`--effort`。模型和推理强度通常沿用 agy 配置，用户指定时通过 `agy models` 核实可用值。`--agent` 选择 agy 自身的 agent 配置，`--role` 是本脚本的任务标签。

子任务沿用用户已经授予当前任务的工作范围。脚本通过 agy 自身执行工具操作，父代理的 sandbox 和审批设置需要单独考虑。`plan` 和提示词里的只读要求属于行为约束，权限隔离由运行环境和 agy 配置决定。只有用户明确要求跳过 agy 权限检查时才传入 `--skip-permissions`。遇到登录、权限或配额失败，读取错误并解决对应原因，保持授权范围。

## 等待与收集

```bash
python3 "$AGY_RUNNER" status TASK_ID
python3 "$AGY_RUNNER" list
python3 "$AGY_RUNNER" wait TASK_ID --timeout 30
python3 "$AGY_RUNNER" result TASK_ID
```

`wait` 最多等待 60 秒，超时返回当前状态，任务继续运行。可以在等待间隙处理独立工作。用户要求完整交付时，持续跟进到全部相关任务结束。`completed`、`failed`、`timed_out`、`interrupted`、`closed` 为终止状态。

`result` 返回任务信息、结构化 agy 结果和 `artifacts_dir`。最终回答位于 `output.result.response`。失败时查看 `output.error`、该目录中的 `stderr.log`、`events.ndjson` 和 `supervisor.log`。原始事件可用于了解执行进展。空回答、缺少 result 事件或失败状态会记为 `failed`，保留日志供诊断。

对结论检查文件和证据。对代码修改检查实际 diff 并执行适用的验证。父代理负责处理任务间冲突和后续修复。

## 续接、中断与关闭

```bash
python3 "$AGY_RUNNER" send TASK_ID --prompt-file /absolute/follow-up.txt
python3 "$AGY_RUNNER" interrupt TASK_ID
python3 "$AGY_RUNNER" close TASK_ID
```

`send` 在同一 conversation ID 上启动下一轮，保留任务目录与配置。它要求前一轮已经结束，运行中的任务需先等待或 `interrupt`。需马上改变方向时，中断后发送补充指令。首次初始化前中断可能尚无 conversation ID，此时用已知背景启动新任务。

`interrupt` 请求停止该任务当前的 agy 进程组，保留日志和已获得的 conversation ID。已发生的文件修改仍需检查。`close` 在停止当前轮次后封闭本地句柄，保留 agy 会话及本地记录。关闭的句柄可用于读取结果，后续新任务使用 `spawn`。

每轮的事件、提示词和结果默认保存在 `~/.local/state/agy-subagent/`。可通过顶层参数 `--state-dir` 或环境变量 `AGY_SUBAGENT_STATE_DIR` 更改位置，同一个任务的操作需使用相同位置。共享状态目录中的操作通过文件锁串行化。重试有副作用的失败任务前，检查实际修改和日志。

需要了解与 Codex 子代理的能力对应、兼容性或实现机制时，阅读 [references/behavior.md](references/behavior.md)。
