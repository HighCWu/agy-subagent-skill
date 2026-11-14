# 能力对应与运行协议

## 与 Codex 子代理对应

Codex 提供任务创建、追加指令、等待和关闭，以及角色、模型配置与多代理协作。本技能以本地 Python 进程和 agy 会话组合提供这些工作流。Codex 官方文档说明原生子代理继承父代理的权限。本技能启动独立 agy 程序，执行权限由 agy 和外部运行环境控制。

| Codex 操作或概念 | 本技能接口与语义 |
| --- | --- |
| spawn_agent | spawn，立即返回任务 ID，后台执行一轮 |
| list_agents | list 或 status，读取本地运行状态 |
| wait_agent | wait，有界等待，调用后继续监控 |
| send_message / followup_task | send，通过 --conversation 精确续接，前一轮需结束 |
| interrupt_agent | interrupt，停止当前轮次，可保留会话续接 |
| 关闭任务 | close，停止执行并封闭本地句柄，保留记录 |
| default / worker / explorer / reviewer | --role，任务标签与默认执行模式，具体职责写入提示词 |
| 模型和推理强度 | --model 与 --effort，沿用配置或按用户要求选择 |
| 继承上下文 | 父代理显式提供相关背景、文件和规则 |
| 并行共享工作区 | 独立 spawn，在提示词中分配编辑所有权 |

运行中即时消息和自动继承 Codex 的工具、MCP、沙箱策略属于原生运行时能力。这里的追加指令在上一轮结束后，以同一 agy conversation 的新轮次执行。脚本运行状态通过 shell 返回。任务可在父代理命令退出后继续执行，监控和结果整合仍由父代理完成。

## 已验证的 agy 协议

以本机 `agy --help` 为接口依据。实测 `--print`、`--output-format json` 与 `--output-format stream-json` 均返回结构化结果。脚本使用流式接口。

```json
{"event":"init","conversation_id":"conversation-id","init":{}}
{"event":"step_update","step_update":{"state":"ACTIVE","text_delta":"..."}}
{"event":"result","result":{"conversation_id":"conversation-id","status":"SUCCESS","response":"answer","duration_seconds":3.7,"num_turns":1,"usage":{}}}
```

事件为逐行 JSON。脚本保存全部 stdout 并忽略未知事件，init 出现后立即保存 conversation ID，result 出现后判断轮次状态。成功需要 `status` 为 `SUCCESS` 且 `response` 为非空文本。顶层的进程退出码供诊断使用，应用层结果保存在 `result.json`。

每轮单独启动 agy，续接显式传入 `--conversation ID`。为避免并行任务串到其他会话，使用精确 ID。`--disable-slash-commands` 使提示词作为普通任务文本处理。脚本自行执行每轮超时，向 agy 传入 `--print-timeout 0`。接收 result 后清理剩余 CLI 进程，避免 CLI 退出清理阻塞父任务。

后台 supervisor 与 agy 各自创建进程会话。中断通过本轮目录里的取消标记交给 supervisor，supervisor 向其创建的 agy 进程组发送 SIGTERM，最多等待三秒后使用 SIGKILL。中断保存已有记录，副作用需要根据工作目录检查。运行环境需允许子进程后台存活，在整组进程被宿主回收的环境中需重新确认执行结果。

`--mode plan` 在实测 init 中对应 `request-review` 权限模式。`accept-edits`、`auto` 和 `--sandbox` 的具体权限行为应以已安装版本及本地设置为准。父代理应按实际环境满足任务的执行边界。

本地状态含提示词、事件和回答，默认新建目录权限为 0700。已有目录沿用原权限。关闭保留记录，清理时仅删除已确认完成且无需保留的任务目录。

## 来源

- [Codex Subagents 官方文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)，核实角色、编排、上下文和权限行为。
- [sysCat64/codex-agy-plugin](https://github.com/sysCat64/codex-agy-plugin)，参考其采用本机 agy print 模式的集成方式。此技能的生命周期管理脚本为独立实现。
- 本机 `agy --help`、`agy help acp`、`agy models` 和真实 print/续接运行，核实 CLI 参数与事件结构。
