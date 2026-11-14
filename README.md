# agy-subagent-skill

在 Codex 中通过 `spawn_agent`、`send_message`、`followup_task`、`wait_agent` 等操作委派 Antigravity CLI（`agy`）任务。参数采用与 Codex collaboration 接近的 JSON 结构。

## 安装

需要已安装并登录的 agy、Python 3.10+ 和 Linux/macOS。

```bash
git clone https://github.com/HighCWu/agy-subagent-skill.git
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R agy-subagent-skill/agy-subagent "${CODEX_HOME:-$HOME/.codex}/skills/"
```

在新的 Codex 聊天中调用 `$agy-subagent`，或让 Codex 使用 agy 委派具体工作。

```text
用 $agy-subagent 创建一个 explorer 调查缓存失效原因，继承当前聊天上下文。主代理继续检查调用方，等待结果后整合修复。
```

## 接口

```bash
runner="${CODEX_HOME:-$HOME/.codex}/skills/agy-subagent/scripts/agy_subagent.py"
python3 "$runner" spawn_agent <<'JSON'
{
  "task_name": "investigate_cache",
  "message": "调查缓存失效原因，只读取文件，返回相关路径和证据。",
  "agent_type": "explorer",
  "fork_turns": "all"
}
JSON
python3 "$runner" send_message <<'JSON'
{"target":"investigate_cache","message":"补充检查并发写入场景。"}
JSON
python3 "$runner" wait_agent <<'JSON'
{"timeout_ms":30000}
JSON
python3 "$runner" followup_task <<'JSON'
{"target":"investigate_cache","message":"请补充边界条件分析。"}
JSON
```

| 操作 | 字段 |
| --- | --- |
| `spawn_agent` | `task_name`, `message`, 可选 `agent_type`, `fork_turns`, `model`, `reasoning_effort` |
| `send_message` / `followup_task` | `target`, `message` |
| `interrupt_agent` / `close_agent` / `resume_agent` | `target` |
| `list_agents` | 可选 `path_prefix` |
| `wait_agent` | 可选 `timeout_ms` |

返回 `agent_id` 和 `/root/investigate_cache` 这样的任务路径，后续可用 ID、相对名或规范路径定位。运行中消息会排队并自动在当前轮结束后投递。空闲代理收到 `send_message` 后保留消息，`followup_task` 启动新工作。等待返回任一新消息或结果，并消费对应邮箱事件。

`fork_turns=all` 默认从当前 Codex 聊天已落盘的用户与助手文本中提取上下文。独立任务使用 `none`。环境缺少聊天记录时可传入扩展字段 `parent_context` 或 `context_file`。父代理的工具和权限由 agy 环境单独处理。

`cwd` 指定工作目录，`agy` 对象承载提供方选项，例如 `{"mode":"accept-edits","timeout":900}`。默认沿用 agy 配置并等待完成。`model` 使用 `agy models` 中的 ID，`reasoning_effort=ultra` 会映射到 agy 的 `max`。`schemas` 返回完整参数定义。

本实现通过本地脚本和 agy print 协议编排。消息在完整轮次边界投递，即时改向可中断后追加任务。操作结果通过脚本返回。详细语义与原生运行时差异见 [技能说明](agy-subagent/SKILL.md) 和 [接口与运行边界](agy-subagent/references/behavior.md)。旧版 CLI 命令和任务记录继续兼容。

## 历史分叉和上下文压缩

新增 `list_turns`、`fork_agent`、`retry_agent` 和 `rewind_agent`。使用 `list_turns` 返回的稳定轮次 ID，可从早期历史探索不同方案，或重新生成某一轮的回答。

```json
{
  "target": "investigate_cache",
  "task_name": "alternative_plan",
  "last_turn_id": "TURN_ID",
  "message": "从这里给出另一种实现方案。",
  "context_mode": "summary",
  "context_summary": "缓存键需要包含租户ID，修改范围为 src/cache.py。"
}
```

将 `TURN_ID` 替换为实际轮次 ID，作为 `fork_agent` 的 JSON 参数传入。`context_mode` 默认 `full`，`summary` 使用调用方提供的摘要，`none` 使用空历史。分支可指定不同 `model` 或 `reasoning_effort`。

`retry_agent` 保留目标轮次之前的历史并复用输入，可通过 `message` 更换提示词。`rewind_agent` 将原句柄回退到 `before_turn_id` 开始前，接着用 `followup_task` 继续。新方向会使用新的 agy conversation，旧历史和消息队列会归档。

这些操作处理聊天上下文，工作目录使用当前文件状态。完整参数和示例见 [历史能力说明](agy-subagent/references/history.md)。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖 JSON 参数、角色指令、上下文分支、聊天隔离、消息邮箱、并发追加、关闭恢复、历史分叉、指定轮次重试、摘要上下文、回退隔离、历史恢复及旧记录迁移。开发中也验证了真实 agy 的运行中排队与同会话续接。

参考 [Codex 子代理官方文档](https://learn.chatgpt.com/docs/agent-configuration/subagents) 和 [sysCat64/codex-agy-plugin](https://github.com/sysCat64/codex-agy-plugin) 的 print 模式集成思路。
