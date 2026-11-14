# agy-subagent-skill

将 Antigravity CLI（`agy`）作为 Codex 的外部任务执行代理，支持后台启动、并行运行、状态查询、等待、结果收集、会话续接、中断和关闭。

## 安装

需要已安装并登录的 `agy`、Python 3.10+ 和 Linux/macOS。

```bash
git clone https://github.com/HighCWu/agy-subagent-skill.git
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R agy-subagent-skill/agy-subagent "${CODEX_HOME:-$HOME/.codex}/skills/"
```

在新的 Codex 聊天中调用 `$agy-subagent`，或让 Codex 使用 agy 完成具体任务。

## 使用

```text
用 $agy-subagent 让 agy 调查缓存失效问题，主代理继续检查调用方，最后合并结论。
```

```text
让 agy 实现 src/cache.py 的修复，由你负责其他文件，等待 agy 完成并验证整体行为。
```

也可以直接使用脚本。

```bash
runner="$HOME/.codex/skills/agy-subagent/scripts/agy_subagent.py"
python3 "$runner" doctor
python3 "$runner" spawn --cwd /absolute/project --role explorer \
  --prompt '调查缓存失效逻辑，只读取文件，返回相关路径与结论。'
python3 "$runner" wait TASK_ID --timeout 30
python3 "$runner" result TASK_ID
python3 "$runner" send TASK_ID --prompt '请补充边界条件分析。'
python3 "$runner" interrupt TASK_ID
python3 "$runner" close TASK_ID
```

`TASK_ID` 使用 spawn 返回的 `id`。续接需要前一轮结束。运行中的任务可先中断再发送新指令。`wait` 超时只返回当前状态，后台任务继续执行。多次 spawn 可并行运行不同任务。

`worker` 默认使用 `accept-edits` 模式，其他角色默认使用 `plan`。提示词中应明确职责、编辑范围、必要上下文与验证方式。agy 使用自身的配置和执行权限。接入 Codex 后，由主代理负责传递任务背景并核实实际结果。

每轮默认限时 600 秒，可用 spawn 的 `--timeout` 调整。日志和结果保存在 `~/.local/state/agy-subagent/`，可用顶层参数 `--state-dir` 或 `AGY_SUBAGENT_STATE_DIR` 更改。关闭任务后记录会保留。脚本依赖 POSIX 文件锁和进程组。

完整说明见 [SKILL.md](agy-subagent/SKILL.md) 与 [能力对应和协议](agy-subagent/references/behavior.md)。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖结构化结果、精确会话续接、中断、超时、并行任务、重复发送竞争、空输出、异常退出和命令参数原样传递。测试使用本地模拟 CLI。

开发时也使用本机 agy 验证了结构化输出与真实多轮会话。适配以当前机器 `agy --help` 和实际事件为依据。上游 CLI 参数或事件协议变化时应重新验证。

## 参考

参考 [sysCat64/codex-agy-plugin](https://github.com/sysCat64/codex-agy-plugin) 的本机 print 模式集成思路，并依据 [Codex 官方子代理文档](https://learn.chatgpt.com/docs/agent-configuration/subagents) 设计任务生命周期。运行脚本为独立实现。
