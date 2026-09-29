# KISS Sorcar 架构参考

**用途：** 把 KISS Sorcar 的结构、算法、提示词原文和数据格式记全。后续做 M3 worktree、M4 界面、M5 多模型时只看这份，不必再翻它约 6 万行源码。
**对照版本：** `ksenxx/kiss_ai` @ `f10bb6f`（2026-09-10），Apache-2.0 许可。下文 `文件:行号` 都相对于 `src/kiss/`。
**永久链接前缀：** `https://github.com/ksenxx/kiss_ai/blob/f10bb6fff1bd985836d5ef2e1601f7df7ac53f70/src/kiss/`
**LocalAgent 采用了哪些、改了哪些：** 见 [architecture.md](architecture.md) 第 7 节。

---

## 0. 总览

### 0.1 五层与类关系
```
core/kiss_agent.py              KISSAgent            L1 单会话 ReAct 循环（Base 的子类）
agents/sorcar/relentless_agent  RelentlessAgent      L2 续跑：每个子会话 new 一个 KISSAgent（组合）
agents/sorcar/sorcar_agent      SorcarAgent          L3 工具层（继承 Relentless）
agents/sorcar/chat_sorcar_agent ChatSorcarAgent      L4 会话 + 持久化（继承 Sorcar）
agents/sorcar/worktree_sorcar_agent WorktreeSorcarAgent L5 git worktree 隔离（继承 Chat）
```
**组合和继承是混用的。** L2 在每个子会话里组合一个 L1；L3、L4、L5 则是一层层继承，每层只管一件事，其余交给上一层。
论文给出的规模：五个核心类只算"有效行"共约 2,924 行。但整个仓库仅 `core/`、`agents/sorcar/`、`server/` 三个目录就有约 6 万行，多出来的主要是 worktree 边界处理、多进程并发、崩溃恢复、多厂商协议。

### 0.2 目录地图
| 文件 | 行数 | 职责 |
|---|---|---|
| `core/kiss_agent.py` | 991 | L1 循环、hook、fallback、轨迹保存 |
| `core/models/model.py` | 2,299 | Model 基类：对话、工具 schema、用量追加 |
| `core/models/model_info.py` | 1,521 | 模型目录、MY_MODELS、按前缀路由、默认/快速模型 |
| `core/models/openai_compatible_model(2).py` | 1,834 / 2,444 | Chat Completions 与 Responses 两种传输 |
| `core/models/anthropic_model.py`、`gemini_model.py`、`claude_code_model.py`、`codex_model.py` | | 各厂商适配、CLI 模型 |
| `core/vscode_config.py` | 1,086 | `~/.kiss/config.json`、自定义 endpoint、API key 文件 |
| `agents/sorcar/relentless_agent.py` | 826 | L2 |
| `agents/sorcar/sorcar_agent.py` | 2,311 | L3 工具组装、`set_model`、`run_parallel`、运行中插话 |
| `agents/sorcar/useful_tools.py` | 1,038 | Read/Write/Edit/Bash 实现 |
| `agents/sorcar/chat_sorcar_agent.py` | 662 | L4 |
| `agents/sorcar/persistence.py` | 3,329 | SQLite |
| `agents/sorcar/worktree_sorcar_agent.py` + `git_worktree.py` + `worktree_pool.py` | 1,810 + 2,716 + 431 | L5 |
| `agents/sorcar/commit_message.py` | 217 | 用 LLM 生成 commit 消息 |
| `agents/sorcar/agent_dispatch.py`、`cron_agent.py`、`mcp_servers.py`、`skills.py`、`web_use_tool.py`、`docker_*.py` | | `run_agent`、定时任务、MCP、Skills、浏览器、Docker |
| `server/web_server.py`、`server.py`、`task_runner.py`、`merge_flow.py`、`json_printer.py`、`commands.py` | 7,738 等 | 守护进程、WebSocket、任务执行、合并流程、事件打印 |
| `SYSTEM.md`、`SYSTEM_LITE.md` | 20KB / 4KB | 系统提示词 |

### 0.3 用户数据目录 `~/.kiss/`
`config.json`（设置）、`api_keys.env`、`MODEL_INFO.json`（安装版的模型目录副本）、`MY_MODELS.json`、`sorcar.db`、`SORCAR.md`（用户级附加指令）、`skills/`、`commands/`（Markdown 斜杠命令）、`mcp.json`、`mcp_auth/`、`MY_TASK_TEMPLATES.md`（欢迎页任务按钮）、`MY_INJECTION.md`（常用指令片段）。

### 0.4 三个前端，一个守护进程
VS Code 扩展、`sorcar` 命令行、浏览器/手机都连同一个本地守护进程 `kiss-web`。命令行走 unix socket（`agents/sorcar/daemon_client.py`），浏览器走 WebSocket（`server/web_server.py`）。所有 agent 都在守护进程里运行。

---

## 1. L1 KISSAgent（`core/kiss_agent.py`）

- **`run()` 的参数：** `model_name, prompt_template, arguments, system_prompt, tools, is_agentic, max_steps, max_budget, model_config, printer, attachments, llm_call_hook, tool_call_hook`。
  - 系统提示词通过 `model_config["system_instruction"]` 传入。
  - 结束时在 `finally` 里调用 `_save()` 保存轨迹。
- **工具：** 就是普通 Python 函数，由签名加 docstring 生成 OpenAI schema，只生成一次并缓存。没有显式传 `finish` 时自动加上（329–348）。
- **主循环** `_run_agentic_loop`（545–605）：
  - 每步先 `_check_limits`，检查预算、全局预算，以及上下文是否达到 `CONTEXT_LIMIT_FRACTION = 0.9`，达到就抛 `ContextWindowExceededError`。
  - 异常处理：
    - 可重试的错误连续最多 3 次（`MAX_CONSECUTIVE_ERRORS`）。
    - 不可重试的错误，先切换到 `fallback` 模型，每次 run 只切一次。
    - 连续空回复或安全拒答，也会切 fallback。
    - 上下文溢出直接抛给 L2。
- **单步** `_execute_step`（607–751）：
  1. `pre_step_hook(model)`：注入运行中插话。
  2. `llm_call_hook`：可改写新消息。
  3. 调模型，拿到 `function_calls`。
  4. 统计 token 和费用。
  5. 用量字符串 `Steps: x/y, Context: a/b tokens, Total tokens: n, Budget: $u/$m` 通过 `set_usage_info_for_messages` **附加到下一条新 user/工具结果消息末尾**（`models/model.py` 919–920、1024–1025）。历史消息从不修改。
  6. 没有工具调用时追加提醒原文：`**Your response MUST have at least one function call. Your response has 0 function calls. If you have completed the task, call the finish tool now with your final result instead of describing it in text.**`。连续 2 次只回文字（`MAX_CONSECUTIVE_NO_TOOL_CALLS`），就以最后一段文字隐式 finish。
  7. 逐个工具调用：先过 `tool_call_hook`（返回值不是 "OK" 就拦截），再过 `tool_call_guard`，然后执行。异常信息连同期望签名一起作为结果返回给模型。
- **非 agentic 模式：** 单次生成，用于 commit 消息、摘要这类辅助调用（`_run_non_agentic`）。
- **运行到底型模型（`cc/*`、`codex/*`）：** 整个任务一次交给 CLI，系统提示词拼进任务文本（406–440）。
- **fallback：** 用 `MODEL_INFO[model].fallback` 新建模型，复制对话，重建工具 schema，发一条 `system_prompt` 事件说明切换（484–543）。

## 2. L2 RelentlessAgent（`agents/sorcar/relentless_agent.py`）

### 2.1 系统提示词和首条消息的拼装（420–433、326–367）
```
system = SYSTEM.md
       + IMPORTANT_INSTRUCTIONS(work_dir, pid)
       + "# Task Settings"：模型名、预算、开始时间（精确到秒，含时区）、user、IP、OS、机器、
                           并行模式、worktree 模式、chat id、task id、是否子代理、父任务 id
       + ~/.kiss/SORCAR.md（有就追加）
首条 user 消息 = TASK_PROMPT = "{task_description}\n\n{previous_progress}"
```
⚠ Task Settings 里有开始时间、task id 等每个任务都会变的值，所以**跨任务的系统提示词前缀无法复用**。对云端 prompt caching 影响有限，对本地 KV 前缀缓存代价很大。

### 2.2 提示词原文
```
# MOST IMPORTANT INSTRUCTIONS
- **If the task is not complete and you are at risk of running out of context length, you MUST call
  finish(success=False, is_continue=True, summary_in_html="precise chronologically-ordered list of things
  the agent did with the reason for doing that along with relevant code snippets, formatted as HTML ...")**
- The summary_in_html argument of finish MUST always be formatted as HTML.
- Work dir: {work_dir}
- Current process PID: {current_pid} — NEVER kill this process.
```
```
# Task Progress (Continuation {continuation_number})
{progress_text}
# Continue
- Complete the rest of the task.
- **DON'T** redo completed work.
- If you have been retrying the same approach without progress, step back and rethink the strategy from scratch.
```
```
# Summarizer
The executor's trajectory is saved at: {trajectory_path}
Read relevant portions of the file using your tools:
- Read the first ~50 lines to understand the task and system instructions.
- Read the last ~200 lines to see the most recent steps and outcomes.
- Do NOT read the entire file; it may be very large.
# Instructions
- Analyze the trajectory file.
- Return a precise chronologically-ordered list of things the agent did with the reason for doing that along with relevant code snippets.
- Format the summary as HTML ... - Call finish(result="detailed summary of work done so far, in HTML").
```

### 2.3 流程（`perform_task`，388–590）
```
summaries = []
for session in range(max_sub_sessions):            # 默认 10000
    executor = KISSAgent(...)                       # 全新对话
    try: result = executor.run(system, TASK_PROMPT(task, progress_section), tools=[finish, *tools],
                               max_steps, max_budget=剩余预算, hooks...)
    except ContextWindowExceededError / 其他 KISSError（且 step_count > 1）:
        轨迹写入 work_dir/tmp/trajectory_{session}.json
        Summarizer = KISSAgent(tools=[Read, Bash]) 读轨迹 → 摘要        # 冷启动 + 多步
        result = finish(success=False, is_continue=True, summary)
    except 其他（带 __cause__ 的错误，或第 1 步就失败）: 直接返回失败
    payload = yaml(result)
    if not is_continue or success: 多会话时把各段摘要合并成 "<h3>Previous Session N</h3>…" + Final Session；返回
    summaries.append(summary); progress_section = CONTINUATION_PROMPT(_capped_progress_text(summaries))
raise "Task failed after N sub-sessions"
```
- **`_capped_progress_text`（151–183）：** 优先保留**最新**的摘要，总长 ≤ 60,000 字符（`MAX_PROGRESS_CHARS`）。丢弃的部分前面加一行 `(N earlier attempt summaries omitted.)`。
- **两种续跑方式：**
  - 主动：模型按提示词在上下文约 80% 时（SYSTEM.md 里的写法）自己调用 `finish(is_continue=True)`。
  - 被动：达到 90% 时抛异常，由 Summarizer 从轨迹文件写摘要。
- **预算：** 每个子会话的预算 = 剩余预算；`_check_total_budget` hook 负责全局检查。

## 3. L3 SorcarAgent（`agents/sorcar/sorcar_agent.py`、`useful_tools.py`）

### 3.1 工具清单（1439–1471）
Read、Write、Edit、Bash，外加：网页工具（Playwright Chromium：`go_to_url`、click、type、screenshot、`show_browser` 等）、`run_parallel`、`number_of_cores`、`ask_user_question`、`talk`（语音）、`set_model`、`summary`（空操作，只供 UI 折叠）、Skills、MCP 工具、`run_agent`（第三方 agent / SEA 文件 / cron）。Docker 模式下 Bash 等换成在容器里执行的版本。

### 3.2 四个核心工具
| 工具 | 细节 |
|---|---|
| `Read(file_path, max_lines=2000, start_line=1)` | 目录→列出内容；二进制按 MIME 作为附件（≤20MB）；截断时注明 `[truncated: N more lines]`；找不到文件时给出相近文件名；**不带行号** |
| `Edit(file_path, old_string, new_string, replace_all=False)` | 精确匹配；匹配不到时试一次 CRLF；默认要求唯一；**"先读后改"只写在提示词里，代码不检查** |
| `Write(file_path, content)` | 整个文件覆盖写入 |
| `Bash(command, description, timeout_seconds=300, max_output_chars=50000)` | 流式输出；超时杀进程组（Windows 用 `taskkill /T /F`）；shell 退出后留在后台的子进程不杀；输出保留头尾截断；在 worktree 里禁止引用父仓库路径 |

⚠ Windows 下它用 `shutil.which("bash")` 找 bash，本机会先找到 System32 里的 **WSL** bash。

### 3.3 `run_parallel`（991–1049、1892–2130）
参数是 `tasks`（JSON 字符串列表）和 `max_workers`。每个任务起一个 `ChatSorcarAgent`，放进线程池并行跑。子代理**沿用父代理当前的模型**和父代理的 chat，预算按份平分。结果按输入顺序返回 YAML 列表。**不按 endpoint 限制并发。**

### 3.4 `set_model`（1298–1437）
换成新模型实例：复制对话和用量、删掉其他厂商特有的参数、重建工具 schema，但不写入用户偏好。**如果当前在自定义 endpoint 上，切换后仍会带着这个 base_url**（1385–1391）。

### 3.5 运行中插话（1754–1826）
- `pre_step_hook` 从 printer 取出排队的消息，每条追加为 user 消息，原文：`User says: {msg}. Take the message into account and finish your task.`
- `tool_call_guard`：有未处理的消息时拒绝 `finish`，原文：`Error: finish rejected — the user sent a new message while you were working. It will be appended to the conversation at the start of your next step; take it into account before finishing.`

## 4. 模型系统

- **模型目录：** 内置 `MODEL_INFO.json`（504 个模型），再用 `~/.kiss/MY_MODELS.json` 覆盖或追加（`model_info.py` 157–192、455–475）。
  - 每个模型的字段：`context_length`、`input_price_per_1M`、`output_price_per_1M`、`fc`、`emb`、`gen`、`thinking`、`use_responses_api`、`fallback`、`alias_of`，以及各项缓存价格。
- **路由看模型名前缀：**
  - OpenAI 兼容类（550–606）：`openrouter/`、`gpt*`/`o3*`、`Qwen/` 等（Together）、`glm-`、`kimi-`。
  - 原生 SDK/CLI（632–637）：`claude-`、`gemini-`、`cc/`、`codex/`。
  - API key 从环境变量或 `~/.kiss/api_keys.env` 读取。模型选择器只列出配了 key 的厂商下的模型。
- **自定义 endpoint 全局只有一组：** `custom_endpoint`、`custom_api_key`、`custom_headers`（`core/vscode_config.py` 67–78）。
  - 在模型列表里显示成 `custom/<URL 最后一段>`。
  - 只要设置了，`task_runner.py` 1377、1467 行就会把 `base_url` 塞进**每个任务**的 model_config，而工厂函数 `model()` 一看到 `base_url` 就跳过路由（1112–1141）。
  - ⇒ 本地模型和云端原生 SDK 模型无法在同一个任务里混用（读代码得出的结论）。
- **"给 agent 选模型"有四个层次：**
  1. 每个标签页各选一个模型（`server.py:588` 的 `_tab_models`；上次选择存在 `config.json` 的 `last_model`）。
  2. 任务中途用 `set_model` 切换。
  3. 辅助角色写死：commit 消息用 `get_fast_model()`，按 Anthropic → OpenAI → Gemini → OpenRouter → Together → CLI 的顺序取第一个配了 key 的。
  4. 扩展代理文件（SEA）里定义 `get_model()` / `get_model_config()`（`server/README.md` 136–150）。
- **默认模型：** `get_default_model()` 按同样的厂商顺序选，例如有 Anthropic key 时是 `claude-opus-4-7`。

## 5. L4 ChatSorcarAgent 与持久化

### 5.1 历史怎么拼进提示词（`build_chat_prompt`，200–226）
```
## Previous tasks and results from the chat session for reference

### Task 1
{task}

### Result 1
{result}
...
---

# Task (work on it now)

{prompt}
```
- 最多 10 条（`MAX_TASKS = 10`）。超过时执行 `del chat_context[2:2 + len - 10]`，也就是**保留最前 2 条和最新 8 条**。
- 历史**放在第一条 user 消息里**，不进系统提示词。结果文本本身不截断。
- **历史的两种来源：**
  - `_load_chat_context(chat_id)`：这个 chat 的全部任务，排除子代理的行。
  - `_load_task_chain_context(task_id)`：从某个任务沿 `parent_task_id` 一路往上。用于从历史记录里点开某个任务后接着做。
- **`run()` 的流程（411–662）：**
  1. 分配 chat_id，拼好提示词。
  2. `_add_task` 提前写入一行（记录模型、工作目录、版本、并行、worktree、预算、自动提交、开始时间）。
  3. 通知 printer。
  4. 记录为常用任务（子代理不记）。
  5. 调用 `super().run()`。
  6. 在 `finally` 里保存结果摘要，以及 token、费用、步数。
  7. 如果这次运行没有产生事件，补写 prompt 和 result 事件，保证能回放。

### 5.2 数据库 `~/.kiss/sorcar.db`（`persistence.py` 766–828）
| 表 | 主要列 |
|---|---|
| `task_history` | id, timestamp, task, has_events, result, chat_id, model, work_dir, version, tokens, cost, steps, is_parallel, is_worktree, owner, max_budget, parent_task_id, extra... |
| `events` | id, task_id, seq, event_json, timestamp；`(task_id, seq)` 唯一 |
| `model_usage` | model, count |
| `file_usage` | path, count, last_used（给 @ 文件补全用） |
| `frequent_tasks` | task, count, timestamp |

**并发处理：**
- WAL 模式、每个线程一个连接、写者优先的读写锁。
- 读-改-写用 `BEGIN IMMEDIATE`，保证跨进程也是原子的。
- 事件由后台线程批量写入，攒满 256 条或每 20ms 写一次。
- **所有流式 delta 都会写入数据库。**

## 6. L5 WorktreeSorcarAgent（`worktree_sorcar_agent.py`、`git_worktree.py`、`worktree_pool.py`）

### 6.1 生命周期
```
run(task):
  1. 读取 auto-commit 设置（每次都重新读）
  2. _retire_previous_worktree():
       上个任务失败或被停止（_pending_review）→ preserve：提交到它自己的分支、删目录、保留分支，绝不合并
       上个任务正常完成（用户没点任何按钮）→ release：auto-commit → squash 合并回原分支 → 删分支
  3. _try_setup_worktree(repo):
       原分支 = 当前分支（detached HEAD → 退回直接执行）
       取一个 worktree：优先用预建池里的备用（reset 到原分支 tip + 清理 untracked + 去掉备用标记），
                        否则先回收孤儿 worktree、清理残留 config，再 git worktree add
       分支名 kiss/wt-*，目录 <repo>/.kiss-worktrees/kiss_wt-*（写进 .git/info/exclude）
       在 git config branch.<分支>.* 里记下原分支；owner pid 在创建时写入
       copy_dirty_state：把主目录未提交的改动复制进来 → 提交 "kiss: baseline from dirty state"，记下 baseline
       work_dir 改指到 worktree 里对应的子目录
  4. ChatSorcarAgent.run()
  5. 任务结束后 UI 显示三个按钮：合并 merge() / 原样保留 leave_as_is() / 丢弃 discard()
```
- **什么情况退回直接执行：** `use_worktree=False`、不是 git 仓库、仓库还没有提交、HEAD 是 detached、工作目录不在仓库内、任何一步 git 操作失败。

### 6.2 提交与清理（`_commit_and_clean_worktree`，413–512）
1. 先等被放弃的子代理线程结束，最多等 `_ABANDONED_SUBAGENT_WAIT_SECONDS`。
2. auto-commit：先 `git add -A`，用 LLM 根据 staged diff 生成 commit 消息。
3. 还有漏掉的改动就用通用消息再提交一次（`kiss: auto-commit late-arriving changes`）。
4. 仍有未提交内容 → 保留目录。
5. 把被 gitignore 的新文件抢救到主仓库（不覆盖已有文件）。
6. `git worktree remove`。

结果枚举：`COMMITTED_AND_REMOVED`，以及 `PRESERVED_NO_AUTOCOMMIT` / `PRESERVED_SUBAGENT_ACTIVE` / `PRESERVED_COMMIT_FAILED` / `PRESERVED_RESCUE_FAILED`。

### 6.3 合并（`_do_merge` 583–708，`squash_merge_from_baseline` git_worktree 2108–2206）
在 `repo_lock` 保护下：
1. 如果主目录有未提交改动就 stash；stash 失败返回 `STASH_FAILED`，不碰仓库。
2. 切换到原分支，失败返回 `CHECKOUT_FAILED`。
3. 执行 `git cherry-pick --no-commit baseline..branch`。只有当主分支从创建 worktree 以来没有新提交（HEAD == baseline^）时才加 `-X theirs`，用来消除"脏状态基线"造成的假冲突；如果用户在主分支上有过新提交，就不加，以免覆盖用户的提交。
4. 合成一个 commit，消息由用户 prompt 加任务结果生成（`kiss: merged from <branch>` 是兜底）。
5. stash pop，删除任务分支。

冲突或 hook 拒绝时保留分支，并提示用户手动处理的步骤（包括 `git stash pop`）。结果枚举：`SUCCESS` / `CONFLICT` / `CHECKOUT_FAILED` / `STASH_FAILED` / `MERGE_FAILED`。

### 6.4 commit 消息（`commit_message.py`）
提示词原文：
> Generate a concise git commit message for these changes. The user's task prompt is provided for context — use it to phrase the subject line in terms of the user's INTENT, not just the mechanical diff. Use conventional commit format with a clear subject line (type: description) and optionally a body with bullet points for multiple changes. Do NOT quote or repeat the user prompt — it will be appended separately. Return ONLY the commit message text, no quotes or markdown fences.

- 模型用 `get_fast_model()`。
- 生成后在正文末尾追加 `User prompt:` 和 `Result:` 两段。
- 失败时用 `kiss: auto-commit agent work`。
- 会去掉 LLM 输出首尾成对的引号。

### 6.5 崩溃恢复与并发
- **状态存在 git 本身**（`branch.<分支>.*` 的若干 config 键：原分支、baseline、owner pid、保留标记、备用标记），不用额外的状态文件。
- `reclaim_orphaned_worktrees`：owner 进程已经死掉的 `kiss/wt-*` 会被自动提交并合并，但带"保留待审"标记的不动；正在运行的 agent 和备用 worktree 除外。`sweep_orphaned_state` 清掉残留的 config 节。
- **锁：**
  - 同一进程内用每个仓库一把的 `RLock`。
  - 跨进程用 `fcntl.flock`；**Windows 上没有 fcntl，跨进程锁直接失效**（git_worktree 43–56）。
- **worktree 预建池：** 后台线程提前建好一个备用 worktree，省掉任务开始时 `git worktree add` 的全量检出时间。

## 7. 守护进程与前端协议

- **命令**（WebSocket JSON，按功能分组，`server/sorcar.py` 的 ApiCommand 列表）：
  - 任务：`run` / `submit` / `appendUserMessage`（运行中插话）/ `stop` / `userAnswer`（回答 ask_user）
  - 标签页和会话：`newChat` / `openTab` / `closeTab` / `getTabsState` / `resumeSession` / `ready`
  - 历史：`getHistory` / `getAdjacentTask` / `getFrequentTasks` / `deleteFrequentTask` / `setFavorite` / `getInputHistory`
  - 模型和配置：`getModels` / `selectModel` / `getConfig` / `saveConfig` / `getDefaultModel` / `updateModels`
  - 文件：`setWorkDir` / `getFiles`（@ 补全）/ `recordFileUsage` / `openFile` / `checkPaths`
  - git：`worktreeAction`（merge/discard/do nothing）/ `mainTreeAction` / `generateCommitMessage` / `autocommitAction`
  - 其他：`complete`（输入框里的灰色补全建议）/ `shareChat` / 语音 / 更新 / `auth`（远程访问密码）
- **事件：**
  - 文本流：`prompt`、`text_delta`、`text_end`、`thinking_start` / `thinking_end`
  - 工具：`tool_call`、`tool_result`、`system_output`（Bash 流式输出）
  - 状态：`usage_info`、`result`、`task_settings`、`status`、`task_done` / `task_error` / `task_stopped` / `task_interrupted`、`stop_ack`
  - worktree：`worktree_created` / `worktree_progress` / `worktree_result` / `worktree_done`、`autocommit_progress` / `autocommit_done`、`main_tree_done`
  - 界面：`notification`（toast，可以原地更新）、`followup_suggestion`、`tasks_updated`、`tabs_state`、`models`、`history`
- **标签页：** 每个标签页一个 agent。事件带 `taskId` / `tabId`，发给所有正在看这个任务的窗口（多窗口镜像）。
- **界面功能：**
  - 欢迎页任务按钮（`MY_TASK_TEMPLATES.md`）
  - 常用指令片段（`MY_INJECTION.md`），输入框里用 Tab 接受灰色补全
  - @ 文件提及，按使用频率排序
  - 常用任务一键重跑
  - "建议的下一步"：`finish(suggested_next_task=...)`
  - 实时费用和 token 显示、分享对话、远程访问密码、语音唤醒

## 8. SYSTEM.md 要点（逐节）
| 节 | 要点 |
|---|---|
| identity | 身份；追求高质量、不写"AI 空话"（有清单）；规则优先级：安全 > 标了 MANDATORY/CRITICAL 的规则 > 用户任务 > 其他 |
| visibility_constraint | 用户只看得到 `finish(summary_in_html=...)` 和语音；摘要必须是实际内容而不是转述，用 HTML；附带 `suggested_next_task`；超过约 800 字就写到 ./reports 的 HTML 报告里 |
| tool_rules | Write 用于新文件，Edit 用于小改动（一个文件最多 3 处）；长命令用 `nohup cmd > ./tmp/out.log 2>&1 < /dev/null &` 放后台后轮询日志；大文件分段读；临时文件放 ./tmp；上下文用到约 80% 时 `finish(is_continue=True)`；每 10 步调用一次 `summary` |
| web_research | 默认先搜索；一次研究至少访问 10 个站点，边看边在 ./tmp 里记笔记 |
| code_style | 简单、少间接层；优先具名函数而不是闭包；消除冗余；公开方法写 docstring；修根因 |
| workflow | 第一步必须 Read ./SORCAR.md；修改前必须先 Read；修 bug 先写端到端测试复现；讨论深度工作和复杂任务规划（3 个以上文件先列清单）；浏览代码时在 ./tmp 里做笔记 |
| testing | 只写端到端测试，不用 mock，不写检查源码结构的测试；新代码力求 100% 分支覆盖；只跑受影响的测试；跑整套测试时按 CPU 核数分片并行（`run_parallel`）；lint 只在结束时跑一次 |
| pre_finish_verification | 逐条核对需求；同一种修法失败 3 次就换思路 |
| sorcar_specific | 维护 ./tmp/PROGRESS.md 作为跨会话日志；结果里不展示 worktree 路径；不可逆的高影响操作要先确认 |

## 9. 给 LocalAgent 的启示：好的做法与问题
**好的做法：**
- 对话只追加，用量信息附在最新消息后面。
- 用"按时间顺序、带代码片段的摘要"续跑，而不是压缩上下文。
- 历史保留最前 2 条和最新 8 条。
- 工具名沿用 Claude Code 那一套（模型训练时见过）。
- 运行中插话，并且有未处理消息时不许 finish。
- worktree 用 baseline commit 加 cherry-pick 合并；状态存在 git config 里。
- 失败或被停止的任务只提交到自己的分支，绝不合并进用户分支。
- commit 消息附上用户 prompt 和结果。
- 抢救被 gitignore 的产物。

**不适合本地模型或 Windows 的地方：**
- 系统提示词含每个任务都变的值，前缀缓存失效。
- 被动续跑时起一个 Summarizer 读轨迹文件，要冷启动加多步。
- 系统提示词加工具定义约 1.2 万 token。
- Bash 最多返回 5 万字符。
- "先读后改"没有在代码里强制。
- 自定义 endpoint 只有一个，而且会一直带到后续模型上。
- 没有按 endpoint 限制并发。
- 流式 delta 全部写库。
- Windows 下会找到 WSL 的 bash，跨进程锁也失效。
