# 历史分叉、重试与回退

这些扩展从本地保存的任务输入和最终回答重建上下文，并开启新的 agy conversation。父代理可选择完整记录、自己提供的摘要，或空历史。

## 操作

| 操作 | 必填字段 | 可选字段与含义 |
| --- | --- | --- |
| `list_turns` | `target` | `include_content` 返回输入、回答及初始上下文，`include_discarded` 同时显示已移出当前历史的轮次 |
| `fork_agent` | `target`, `task_name`, `message` | `last_turn_id` 保留到该轮结束，省略时使用当前历史的最近已完成轮次 |
| `retry_agent` | `target`, `task_name`, `turn_id` | 从该轮开始前的上下文重新尝试，默认复用该轮输入，`message` 可替换输入 |
| `rewind_agent` | `target`, `before_turn_id` | 将同一代理回退到该轮开始前，进入 `ready`，随后用 `followup_task` 继续 |

历史轮次通过稳定的 `turn_id` 定位。`list_turns` 中的 `turn` 为当前有效历史的位置，已丢弃记录的 `turn` 为 null。`list_agents` 中的 `turn` 保留累计执行次数，新的尝试会继续递增。优先使用 ID 进行后续操作。

`fork_agent` 和 `retry_agent` 创建新任务，继承源代理的角色、工作目录、模型、推理强度与 agy 配置。可通过 `model`、`reasoning_effort`、`cwd`、`agy` 覆盖对应配置。`task_name` 使用通常的命名规则，新分支创建在调用方的任务路径下。

显式的 `last_turn_id` 要求该轮已完成。源代理可以继续运行较新的轮次，早期完成的记录可同时用于分叉。`retry_agent` 也支持已失败或中断的轮次。正在运行的目标轮次需要先中断。

## 上下文选择

三种操作都支持 `context_mode`。

- `full` 为默认值，传入选定边界之前的初始上下文、输入和回答。
- `summary` 仅传入 `context_summary`。调用方根据任务自行整理摘要，保留继续工作需要的约束、事实和文件位置。
- `none` 使用空历史，适合独立重试或重新分配任务。

摘要模式要求提供非空 `context_summary`。完整历史和空历史模式使用对应的已有记录。摘要分支以摘要作为新的历史起点，后续分支继续沿用这个起点。

返回值中的 `context_chars` 是序列化后的历史文本字符数，`replayed_turns` 是复制到新分支的完整历史轮次数。摘要模式和空历史模式的该值为 0。agy 自身的运行指令与工具定义仍计入模型上下文。

## 示例

先调用 `list_turns` 获取实际 `turn_id`，将下面的 `TURN_ID` 替换为对应值。

```bash
python3 "$AGY_RUNNER" list_turns <<'JSON'
{"target":"investigate_cache"}
JSON
python3 "$AGY_RUNNER" fork_agent <<'JSON'
{"target":"investigate_cache","task_name":"alternative_plan","last_turn_id":"TURN_ID","message":"基于此前已确认的事实，给出另一种实现方案。"}
JSON
python3 "$AGY_RUNNER" retry_agent <<'JSON'
{"target":"investigate_cache","task_name":"retry_with_summary","turn_id":"TURN_ID","context_mode":"summary","context_summary":"已确认缓存键缺少租户ID。目标是跨租户隔离，修改范围为 src/cache.py。","message":"基于摘要重新分析并提出修复。"}
JSON
python3 "$AGY_RUNNER" rewind_agent <<'JSON'
{"target":"investigate_cache","before_turn_id":"TURN_ID"}
JSON
python3 "$AGY_RUNNER" followup_task <<'JSON'
{"target":"investigate_cache","message":"从这里改用另一条思路继续。"}
JSON
```

## 历史、并发与恢复

分叉时复制历史快照，源代理后续的追加、回退和关闭分别作用于各自句柄。新分支在新 conversation 中启动，后续正常消息继续使用该分支的 conversation ID。

回退需要源句柄处于空闲状态，关闭的句柄先通过 `resume_agent` 恢复。回退操作归档旧指针与待投递消息，并清空当前消息队列，避免旧计划中的补充指令进入新方向。归档位于任务目录下的 `rewinds/`。原始 request、result 和历史快照继续保留。

通过 `list_turns(include_discarded=true)` 可找到归档轮次。`fork_agent` 和 `retry_agent` 可以使用这些保存的 ID 重新建立分支。旧版本任务会按原始 request 记录迁移，原有包装提示词会保留在旧输入中。

每次回退递增 `history_revision`。结果事件包含 `turn_id` 和 `history_revision`，`wait_agent` 会给已被后续回退替代的结果标记 `superseded=true`，供父代理识别历史结果。新轮次结果使用当前 revision。嵌套代理默认继承父代理的当前有效历史。

回退与分叉的范围是聊天上下文。后续工具操作使用调用时的工作目录文件状态。比较会编辑文件的分支时，通过不同 `cwd` 或明确的文件责任来安排实验。

本地重建的历史包含已保存输入与最终回答，agy 的工具执行轨迹和内部状态由新会话重新建立。所有真实工具操作由新一轮任务指令决定。
