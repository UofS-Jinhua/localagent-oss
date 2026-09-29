# LocalAgent 设计方案 v0.3

目标：做一个类似 Claude Code / Codex 的编码代理。以本地 LLM 为主，以后可以加云端 API。功能包括选择工作目录、选择 git 分支、修改代码、运行测试。
参考：KISS Sorcar，详见 [kiss-sorcar-notes.md](kiss-sorcar-notes.md)。

## 1. 实测：一张 2080 Ti 上的 LM Studio（2026-09-11）

测试对象：局域网里一台 LM Studio（RTX 2080 Ti 22GB），模型 Qwen3.8 27B（qwen35 架构，IQ4_XS 量化，加载上下文 122,880，服务端最多 2 个并发）。

| 项目 | 结果 |
|---|---|
| OpenAI 兼容 `/v1/chat/completions` 的工具调用 | ✓ 返回原生 `tool_calls`；简单调用约 3.4 秒 |
| Anthropic 兼容 `/v1/messages` 的工具调用 | ✓ 返回 `tool_use`，说明 Claude Code 也能直接连 |
| 冷启动 prefill 速度 | **约 245 token/秒**（11,752 token 用了 48 秒） |
| 在同一对话末尾追加一轮（前缀缓存命中） | 首 token **0.83 秒** |
| 只改开头一行（前缀缓存失效） | 首 token **48 秒** |
| 生成速度 | 单路 32 token/秒；两路同时生成时各 13–14 token/秒，总吞吐几乎不变 |
| 发第 3 个并发请求 | 服务端排队，不报错；那个请求等了 50 秒才出首 token |
| 并发与 KV 缓存 | Max Concurrent Predictions = 2，**Unified KV Cache 已开启**：两个槽位共用 122,880 的总额度 |
| 思考（thinking） | LM Studio 里默认开启。**可以按请求关闭：** 传 `reasoning_effort: "none"` 后推理 token 为 0，简单问题耗时从 6.5 秒降到 1.0 秒，带工具调用时同样有效。`low`、`reasoning.effort`、`enable_thinking`、`/no_think` 都不起作用 |

### 这些数据带来的设计约束
1. **对话只能追加（硬性要求）。** 已经发出去的消息一个字都不能改。系统提示词里不放时间戳和步数；同一个会话内工具列表固定不变；用量信息像 KISS 那样附在最新一条消息的末尾。实现上，`Conversation` 类只提供 append 方法。
2. **系统提示词加工具定义控制在约 3K token 以内。** 冷启动大约 12 秒。KISS 的完整 SYSTEM.md 加二十多个工具约 12K token，按上面的速度，每开一个新会话或子代理要等约 50 秒。
3. **按 prefill 成本给工具输出设上限。** 每 1K token 的工具输出，要多花约 4 秒 prefill。建议：
   - Read 默认 400 行
   - Bash 输出只保留开头和结尾，共约 8K 字符
   - Grep 限制返回条数
   - 测试失败时只返回摘要，完整日志存到文件里
4. **续跑的代价是一次冷启动。** 摘要加系统提示词大约 7K token，约 30 秒。所以续跑阈值按模型单独配置，本地模型默认约 65%（在超长上下文里，小模型的指令遵循会变差）。
5. **并发按 endpoint 限制。** 这台机器是单 GPU 两个槽位，并发不会提高总吞吐，还可能挤掉主 agent 的前缀缓存。策略是：
   - 主 agent 固定占一个槽。
   - 另一个槽留给辅助任务：摘要、commit 消息、单个子代理。
   - 超出的请求在客户端排队，UI 显示“等待本地槽位”。
   - 因为开了 Unified KV，调度器还要保证**正在进行的所有请求的上下文加起来不超过 122,880**。例如主 agent 用到 90K 时，辅助任务最多给约 25K。
   - 辅助任务尽量安排在主 agent 等工具（例如跑测试）的时候执行。
   - 真正的并行，留给“本地加云端”的组合。
6. **按“思考模型”来设计，思考按请求开关。**
   - LM Studio 里保持思考开启，由我们在每个请求上决定关不关（`reasoning_effort: "none"`）。
   - 默认：主 agent 做规划、改代码时开启思考；摘要、commit 消息、读取测试输出这类辅助任务关闭。
   - 开启思考时 `max_tokens` 给大一些（例如 16K）。否则模型可能在推理中途用完额度，一个工具调用都没发出来。
   - 推理过程在 UI 里可折叠显示。
7. **工具调用的兜底。** 原生 `tool_calls` 已经可用。但仍保留一个兜底解析器，处理模型把调用写成文本的情况（`<tool_call>` 标签或 JSON）。参数先按 schema 校验，出错时把错误信息返回给模型。

## 1.5 实测：子 agent 的取舍（2026-09-12）

同一台服务器，这次加载上下文是 102,400（交接阈值 66,560 = 0.65 × 102,400）。任务：给 `tests/` 下的每个测试文件写一行说明——读完全部测试文件约 60K token 的工具输出。同一个任务跑两次，唯一的区别是有没有 Task 工具。

| | 开启子 agent | 关闭（对照） |
|---|---|---|
| 主 agent 步数 | 3 | 11 |
| 主 agent 上下文峰值 | **8,833** | **57,925**（6.6 倍） |
| 全过程 prompt token | 78,301（主 11,984 + 4 个子 agent 66,317） | 301,416 |
| 墙钟时间 | 301 秒 | 223 秒 |
| 生成速度（首步 → 末步） | 42 → 47 tok/s | 46 → **30** tok/s |
| 首 token（末步 / 最差） | 6.1 秒 / 6.1 秒 | 7.8 秒 / **16.2** 秒 |
| 结果 | 24 个测试文件，正确把 conftest.py、scripted_server.py 认成支撑文件 | 26 行，把那两个也当成了测试 |

三条结论：

1. **子 agent 省的是上下文，不是时间。** 对照组反而更快：主对话是追加式增长，前缀一直命中缓存；而每个子 agent 都要冷启动 prefill 约 15K token（约 60 秒，对应第 1 节的 245 tok/s）。所以 Task 不是用来提速的，派发的理由只能是"这些工具输出不值得留在主对话里"。
2. **省下来的上下文是真金白银。** 对照组的生成速度随上下文单调下降：10K 时 49 tok/s，58K 时 30 tok/s（−39%），首 token 从 2 秒涨到 16 秒。而且它停在 58K，离 66,560 的交接阈值只差一步——同一个 chat 里的下一个任务一开场就要交接。开子 agent 的那次结束时主对话只有 8.8K，后面还能接着干很久。
3. **模型自己会判断什么时候派发。** 同样的提示词下，"查 `quiet()` 的调用点 + handoff 阈值怎么算"（19K 上下文就做完了，答案里的行号全部正确）它一次 Task 都没调；"读完 24 个测试文件"它在同一轮里派了 4 个子 agent，每个读 6 个文件，自己只收了 4 份报告。

同时验证的机制：预算封顶（给 40,000 时子 agent 窗口是 32,768）、子任务作为 `parent_id` 指向调用方的独立任务行入库、子 agent 的工具输出（16.5K 字符）一个字都没进调用方的对话、子任务不出现在 chat 的任务列表里。回归测试见 `tests/test_live.py::test_sub_agent_returns_a_report_not_its_tool_output`。

## 1.6 实测：GPU 服务器上的一张 A100，有请求才启动（2026-09-12）

环境：一张 A100 80GB（驱动 565）；llama.cpp b10931，用 CUDA 12.6 编译；模型是 Qwen3.8 27B 的 Q8_0 GGUF（29 GB，自带 MTP 头），加 F16 视觉投影。上下文 262,144，2 个槽位共用（`--kv-unified`）。前面挂一个 llama-swap（有请求才启动，空闲 15 分钟卸载），笔记本经 ssh 隧道（中间还有跳板机）访问。配置在 `scripts/gpu-server/`，步骤见 [servers.md](servers.md)。

| 项目 | A100（本节） | 2080 Ti + LM Studio（第 1 节） |
|---|---|---|
| 显存占用 | 49.2 GiB（权重 27 GiB + 262K 的 KV 约 17 GiB + MTP、视觉、计算缓冲） | — |
| 从未加载到答完第一个请求 | **13–15 秒**（模型文件已在页缓存里） | — |
| 冷启动 prefill | **1,100–1,300 tok/s**（上下文深度 0 时 1,299，32K 时 1,125；一次发 5 万 token 是 1,130） | 245 |
| 前缀缓存命中（追加一轮） | 首 token **0.6–0.8 秒**（命中 97K 时也是 0.8 秒） | 0.83 秒 |
| 生成，不开 MTP | 40 tok/s（短上下文），36 tok/s（5 万上下文） | 32 tok/s |
| 生成，MTP 草稿 2 个 token | **84 tok/s**（写代码，草稿接受 139/144），54 tok/s（5 万上下文）；`la models test` 经隧道 61 tok/s | — |
| 关思考 | `chat_template_kwargs: {enable_thinking: false}` 有效 | `reasoning_effort: "none"` |
| 视觉 | ✓（开着 MTP 也正常） | ✓ |

端到端：`scripts/demo_fix_bug.py` 7 步、27K token、28 秒（首 token 0.8 秒，生成 79 tok/s）；`pytest -m live` 两个测试 57 秒全过。

几条结论：

1. **prefill 卡在算力上，调参没用。** `--ubatch-size` 从 512 调到 4096、强制走 cuBLAS，结果都在 1,130–1,300 之间。要再快只能换引擎（vLLM 之类），但那类引擎启动要 1–3 分钟，和"有请求才启动"冲突。
2. **第 1 节的约束放宽了约 4.6 倍，但都还在。** 每 1K token 工具输出多花约 0.9 秒 prefill（原来 4 秒）；子 agent 冷启动 15K token 约 13 秒（原来 60 秒）。"对话只能追加"照样是硬性要求：11.7K token 的缓存失效从 48 秒变成约 10 秒，但长对话里失效一次仍要几十秒到一分多钟。
3. **MTP 值得开。** 代码的草稿接受率 97%，短上下文生成速度翻倍；上下文越长收益越小（5 万时 54 对 36）。
4. **空闲卸载的代价是一次完整 prefill。** 空闲计时器实测有效：最后一个请求 15 分钟后，llama-swap 日志出现 `TTL of 900s reached`，显存归零。进程退出后前缀缓存也没了，10 万 token 的对话回来后第一条约要 100 秒。一个任务内部各步之间远不到 15 分钟，所以只有离开一阵再回来的第一条消息会碰到。
5. **有请求才启动，就必须防孤儿进程。** 实测：llama-server 由 `exec` 启动时，`tmux kill-session` 杀掉 llama-swap 后，llama-server 被过继给 1 号进程，继续占着 49 GB 显存，而且再也没有空闲计时器管它。所以 `start-llm.sh` 不用 `exec`，另起一个 `setsid` 守护进程，盯住 llama-swap 和脚本本身。三种情况都实测过，都在 2 秒内释放显存：API 卸载（空闲计时器走的就是这条路）、关掉 tmux 会话、`kill -9` llama-swap。


### 1.6.1 并发：524K 的 KV 池分给 4 个槽位（2026-09-13）

原来的配置是 262K 分给 2 个槽位，只占 49 GiB，80 GB 的卡还空着 30 GB。在另一张空闲的 A100 上同时起了一个测试实例（没碰正在用的那个），两种配置各测一遍：

| | 262K，2 个槽位（原来） | 524K，4 个槽位（现在） |
|---|---|---|
| 显存 | 49.2 GiB | 69.5 GiB |
| 1 个请求 | 82 tok/s | 81 tok/s |
| 2 个同时 | 共 104 tok/s（每路 56） | 共 101 tok/s（每路 54） |
| 4 个同时 | — | **共 148 tok/s**（每路 42） |
| 单个对话上限 | 262K | 262K（llama.cpp 日志 `n_ctx_slot = 262144`；`n_ctx_seq > n_ctx_train` 的警告针对的是整个池，不是单个对话） |

几条结论：

1. **A100 上并行确实能提高总吞吐，和 2080 Ti 不一样。** 第 1 节里 2080 Ti 两路并发的总吞吐几乎不变；A100 上 4 路是单路的 1.8 倍。单路速度不受槽位数影响。
2. **多出来的 20 GB 用来放 KV，而不是换更大的模型。** 524K 的池能同时放下两个满长度的对话，或者一个 17 万 token 的主 agent 加一个 13 万 token 的子 agent，再加另一台电脑的任务。
3. **槽位多了也挡不住长 prefill 的阻塞。** 一个请求在 prefill 10.2 万 token 时，另一个只要生成 400 token 的请求等了 111 秒（单独跑 5.9 秒）：生成速度本身只从 80 降到 60 tok/s，时间几乎全花在排队等那次 prefill 上（它自己跑在 965 tok/s）。所以几台电脑同时用时，谁发了一个很长的新对话（或者空闲卸载后回来的第一条），其他人都会卡住差不多"token 数 ÷ 1000"秒。
4. **客户端设置随之调整（只对这台 A100 上的模型）：** provider 的 `max_concurrency` 改成 4；模型的 `max_subtask_context` 设为 131,072，子 agent 最多可以用到一半窗口；`max_parallel_subtasks` 设为 3，同一轮里的只读子 agent 同时跑（留一个槽位给调用方，免得它缓存的对话被挤掉）。本地 2080 Ti 上的模型保持默认的 32K 上限：它的池只有 12 万，放不下两个大对话。
### 1.6.2 实测：并行子 agent 并没有更快（2026-09-13）

A100 上 4 路并发的总吞吐是单路的 1.8 倍，于是给 A100 上的 Qwen 开了并行子 agent（`max_parallel_subtasks: 3`）。用真实模型测：同一个任务（给 28 个测试文件各写一行说明），明确要求分成 3 个只读子 agent，只改并行设置：

| | 3 个同时跑 | 一个接一个 |
|---|---|---|
| 总时间 | 307 秒 | 308 秒 |
| 子 agent 阶段 | 220 秒（三个同时开始） | 218 秒（60 + 55 + 102） |
| 每个子 agent 的窗口 | 63,876（平分） | 131,072 |
| 子 agent 合计 token | 17.5 万 | 29.0 万 |
| 主 agent 上下文峰值 | 13.0K | 9.8K |

**结论：一样快。** 1.8 倍只对*生成*成立；子 agent 的活主要是读文件，也就是 *prefill*，而 prefill 卡在算力上（第 1.6 节：调参也只有 1,100–1,300 tok/s），三个同时 prefill 各分到三分之一。读得越多的子 agent，并行越没用；以写为主的子 agent 才会吃到生成那一份提速。

设置保留在 3（没有测出坏处：时间相同），代价是并行时每个子 agent 的窗口只有单独跑时的一半左右。要换回一个接一个，把模型设置里的 parallel sub-agents 改回 1 即可。

另外，同样的任务不强制拆分时，模型**干脆没用子 agent**，自己读完了全部文件：191 秒，主 agent 上下文涨到 7.7 万。262K 的窗口足够大，模型觉得没必要派出去；用了子 agent 的那次 273 秒、主 agent 只到 1.3 万。和第 1.5 节一样：子 agent 省的是上下文，不是时间。

## 2. 分层（对照 KISS）

| 层 | KISS | 我们 |
|---|---|---|
| L1 Agent 循环 | ReAct、`finish`、预算、90% 上下文上限 | 相同。“预算”改成 token 数、步数加耗时；上下文上限按模型配置；对话只追加 |
| L2 续跑 | `finish(is_continue)` 加 Summarizer 读轨迹文件 | 照搬。Summarizer 使用 `summarizer` 角色对应的模型 |
| L3 工具 | Bash/Read/Edit/Write、网页、run_parallel、set_model 等二十多个 | 只保留 Bash/Read/Edit/Write/Grep/RunTests/AskUser/SetModel/Finish。“先读后改”在代码里强制（记录 Read 时的哈希，文件被外部改过就拒绝 Edit）。Bash 固定在工作目录里执行；系统提示词写明工作目录；拦截 `cd ~` 这类跳出工作区的命令（实测模型会主动 `cd ~` 再跑 pytest） |
| L4 会话 | SQLite：tasks/events，历史保留前 2 条加最近 N 条 | 照搬，外加每步的 prefill/生成速度、缓存是否命中 |
| L5 Worktree | `kiss/wt-*`、baseline commit、cherry-pick 合并、合并/保留/丢弃 | 照搬，改名为 `la/wt-*` 和 `.la-worktrees/`。Windows 下用 `msvcrt` 或 portalocker 实现跨进程锁，用 `taskkill /T /F` 杀进程树 |

## 3. 模型注册表（回答“要不要保留添加模型功能”）

**要保留，而且要从一开始就设计好。** KISS 的问题在于路由靠模型名前缀，而自定义 endpoint 全局只能有一个，本地加云端没法混用（见精读笔记 4.3）。我们反过来，**以 endpoint（provider）为中心**：每个模型都属于某个 provider，provider 记录自己的 URL、密钥和并发上限。

```yaml
# ~/.localagents/models.yaml（密钥不写在这里，只写环境变量名或 keyring 条目名）
providers:
  lmstudio-home:
    type: openai_compatible          # LM Studio / Ollama / vLLM / llama.cpp / OpenRouter / DeepSeek …
    base_url: http://192.168.1.20:1234/v1
    max_concurrency: 2
    discovery: lmstudio              # 通过 /api/v0/models 自动读取上下文长度、量化、tool_use
  anthropic:                         # 以后再加
    type: anthropic
    api_key_env: ANTHROPIC_API_KEY
    max_concurrency: 4

models:
  local-qwen27b:
    provider: lmstudio-home
    model_id: qwen3.8-27b
    context_length: 122880
    continue_at: 0.65                # 上下文用到多少时触发续跑
    thinking: toggle                 # 可按请求开关
    thinking_off: {reasoning_effort: none}   # 关闭思考时附加的请求参数（实测有效）
    max_output_tokens: 16384
    price: {input: 0, output: 0}
    notes: "本地，免费；冷启动 prefill 约 245 tok/s，生成 32 tok/s"   # 这段会展示给 set_model，让 agent 自己权衡

roles:            # 在 UI 里把模型分配给各个角色；thinking 可按角色覆盖
  main:           {model: local-qwen27b, thinking: on}
  subagent:       {model: local-qwen27b, thinking: on}
  summarizer:     {model: local-qwen27b, thinking: off}
  commit_message: {model: local-qwen27b, thinking: off}
  reviewer: null  # 以后可以设成云端模型，对应 KISS 的“用 A 写、用 B 审”模式
```

和 KISS 相比的关键差异：
- **路由看 `provider` 字段，不看模型名。** 模型名是用户自己起的别名，同一种 provider 类型可以配任意多个。
- **角色可以配置。** KISS 的 commit 模型、摘要模型是写死的；这里 main、subagent、summarizer、commit_message、reviewer 都能单独指定。
- **`set_model` 只能从注册表里选**，工具说明里带上每个模型的 `notes`。另有工作区级开关“允许发送到云端”，默认关闭，确保代码不会被意外发出去。
- **跨 provider 切换：** 对话在内部用中性格式保存，由各 adapter 转成 OpenAI 或 Anthropic 格式。切换时丢弃上一个模型的 reasoning 块，并提示“新 provider 需要冷启动一次”。

### “添加模型”的交互流程
1. 选择 provider 类型：OpenAI 兼容、Anthropic 或 Gemini（后两个以后再做）。
2. 填写 `base_url`、密钥（可选）和自定义 header。
3. 点“发现”：调用 `GET /v1/models`；如果是 LM Studio，再读 `/api/v0/models`，自动填好上下文长度、量化、是否支持 tool_use、是否已加载。
4. 点“测试”：跑本页第 1 节的几项探针（工具调用、冷启动 prefill、生成速度、前缀缓存），结果写到模型卡片上。
5. 设置价格、并发上限、续跑阈值；然后到“角色”页分配模型。

## 4. 界面

### 4.1 外壳：独立窗口为主，浏览器为辅，共用同一套前端

前端写成 Web 页面（React），由本地 FastAPI 后台服务提供。外壳默认用 **pywebview** 打开独立窗口；同一个页面也能在浏览器里打开。这和 KISS“一个本地后台服务、多个前端”的思路一致。

| 方案 | 形态 | 体积 | 和 Python 后端的关系 | 额外工具链 | 结论 |
|---|---|---|---|---|---|
| **pywebview（WebView2）** | 独立原生窗口 | 几 MB，Win11 自带 WebView2 | 同一进程，Python 直接驱动 | 无 | **采用。** 已实测：pywebview 6.2.1 + Python 3.13.11 + WebView2 152 能正常打开窗口 |
| 浏览器访问 localhost | 浏览器标签页 | 0 | 天然支持 | 无 | **保留为辅。** 可以从手机或另一台电脑通过 Tailscale 查看长任务 |
| Tauri 2 | 独立窗口 | 约 10 MB，外加 Python 子进程 | Python 作为 sidecar 子进程 | Rust | 以后需要安装包、自动更新时再迁移，前端代码不用改 |
| Electron | 独立窗口 | 150 MB 以上 | 子进程 | Node 打包 | VS Code 和 Claude 桌面版都是这种，对我们来说太重 |
| PySide6 / Qt 原生界面 | 独立窗口 | 约 100 MB | 同一进程 | 无 | diff、Markdown、流式输出都得重写，不采用 |

- **独立窗口能做的：** 系统原生的选择文件夹对话框（`create_file_dialog(FOLDER_DIALOG)`）、任务栏图标、Alt-Tab 切换，以及任务完成时的系统通知。
- **浏览器模式的前提：** 后台服务默认只监听 `127.0.0.1`，每次启动生成一个随机 token，页面必须带 token 才能调用接口。因为这个服务能执行 shell 命令，必须防止其他网页跨站调用。开放到 Tailscale 时必须另设密码。浏览器里没有原生选文件夹对话框，改用后端提供的目录浏览器。
- **开发时：** pywebview 窗口直接指向 Vite 开发服务器（`http://localhost:5173`），支持热更新；用 `webview.start(debug=True)` 打开开发者工具。

### 4.2 布局

```
┌ 左栏 ──────────────┬ 中栏 ─────────────────────┬ 右栏（标签页）─────────────┐
│ 工作目录选择器      │ 对话流：                   │ Diff（baseline..worktree）│
│ 基准分支下拉        │  · 可折叠的推理过程         │ 测试结果                   │
│ 模式：worktree/直接 │  · 工具调用卡片、Bash 流式  │ 模型与用量：上下文用量条、 │
│ 会话列表           │  · 运行中插话输入框         │  prefill/生成速度、缓存命中 │
│ 模型 / 角色快捷切换 │ 任务结束：合并 / 保留 / 丢弃 │ 危险命令审批队列            │
└────────────────────┴───────────────────────────┴───────────────────────────┘
```
- KISS 的 diff 审阅依赖 VS Code，所以我们要自己做 diff 视图（Monaco diff editor 或 diff2html）。
- 以下命令默认需要在 UI 上审批：`git push`、`rm -rf`、访问工作目录以外的路径、网络下载。

## 5. 技术栈
- 后端：Python 3.13、FastAPI + uvicorn、WebSocket 事件流、SQLite（标准库）、httpx（OpenAI 兼容）、anthropic SDK（加云端时再引入）。
- 前端：Vite + React + TypeScript + Monaco diff。
- 外壳：pywebview（WebView2）。打包先用 PyInstaller，以后可迁移到 Tauri。
- Windows：Bash 走 Git Bash（`C:\Program Files\Git\bin\bash.exe`）；文件锁和进程树见第 2 节。

## 6. 里程碑
1. **M1 核心（已完成）：** 模型注册表、OpenAI 兼容 adapter（含 endpoint 并发信号量、推理流解析、tool_call 兜底解析）、L1 循环、Read/Edit/Write/Bash/Grep/Glob、命令行界面、`la models test` 探针。
   - 验收：`examples/buggy_stats` 里有两个 bug，agent 用 6 步、58 秒修好，独立跑 pytest 6/6 通过，测试文件没有改动。
   - 观察 1：第一步冷启动 8.3 秒（系统提示词加工具定义约 1.8K token），之后每步首 token 1.3–4.3 秒，说明前缀缓存一直在命中。
   - 观察 2：模型会在同一轮里并行发出多个工具调用。
   - 观察 3：`pytest | tail` 会让 exit code 显示为 0，是管道掩盖了 pytest 的真实返回值。模型能从输出里看出测试失败，但以后可以考虑提示它不要用管道，或者开启 `pipefail`。
2. **M2（已完成）：** L2 续跑、L4 SQLite、运行中插话、RunTests 工具。具体做法和与 KISS 的差异见 [architecture.md](architecture.md) 第 7 节。
   - 在线验证：人为把窗口缩到 5.5K 来强制交接。
     - 修复前：新会话一开始就接近阈值，每个会话只能走一两步；而且新会话不信任笔记、反复验证。结果 6 个会话全部用完，报"未完成"，虽然 bug 其实在第 2 个会话就修好了。
     - 修复后：只带最新一份笔记；每个会话至少能增长窗口的 25%；续跑说明要求相信笔记。
3. **M3（已完成）：** L5 worktree，包括选择 base 分支、baseline、合并/保留/丢弃，以及 diff 和状态 API。和 KISS 的差异见 [architecture.md](architecture.md) 第 7 节第 14–18 行。
   - **做法：** 每个 chat 一个分支 `la/<chat>`，每个任务一个提交，用户明确执行合并才会进入 base 分支。
   - **在线验证：**
     - agent 在分支上修好 bug，用时 61 秒；提交消息由模型撰写。
     - agent 工作期间用户的检出没有被改动。
     - 合并后 base 分支上是一个 squash 提交，测试 6/6 通过。
   - **界面显示分支：** 用 `repo_status()` 取状态，配合 `worktree` / `task_committed` 两个事件，见 architecture.md 第 7.4 节。
4. **M4（已完成）：** 界面：React 前端 + pywebview 窗口，浏览器访问时需要 token。
   - 结构见 [architecture.md](architecture.md) 第 2 节里的 server 和 frontend 两部分。
   - 在线验证：用 LM Studio 的模型在界面里走了一遍运行、插话、完成、审阅、合并；刷新后能从数据库回放；桌面窗口的冒烟测试也通过了。
   - 还没做：在界面里添加和测试模型；"建议的下一步"；@ 文件提及。
5. **M5（已完成）：** 云端 provider、跨 provider 的 `set_model`、界面里管理模型、“允许发送到云端”策略，外加图片和文档附件。
   - **云端接入：** 绝大多数服务（Gemini、OpenAI、Anthropic、DeepSeek、OpenRouter、DashScope）都提供 OpenAI 兼容端点，所以不需要新的 provider 类型，`models/presets.py` 里一条预设即可。密钥存在 `data/secrets.env`，不进 `models.yaml`，也不进 agent 执行命令时的环境变量。
   - **Gemini 实测（2026-09-11，免费额度）：** 工具调用正常，Gemini 3 系列要求把 `thought_signature` 原样带回（OpenAI 兼容层放在 `tool_calls[].extra_content.google`），我们的客户端会保留；对话里如果有别的模型产生的工具调用，就补一个占位签名。看图探针正确认出颜色。列模型走原生接口，不消耗生成额度。
   - **角色跟随服务器：** 角色可以写 `auto`，解析时现查本地服务加载了哪个模型（缓存 10 秒），并采用它实际加载的上下文长度；单卡机器上避免了"写死模型名 → 下一个任务把加载的模型顶掉"。
   - **换模型：** `set_model` 在注册表勾选过的模型之间切换，云端还要打开开关；上下文装不下会拒绝；换过去的模型报错会自动退回任务开始时的模型（实测就遇到了密钥失效，任务因此没有中断）。
   - **附件：** 图片走 `image_url` 内容块，只发给能看图的模型；PDF 抽成文本，Read 分页读、Grep 能搜；小文档直接内联。界面支持粘贴、拖拽、在截图上画框和箭头。存储按内容寻址，附件不进项目的 git 历史。
   - **在线验证：** 本地 qwen3.8-27b 拿到标注过的设计稿后自己调用 `set_model` 换到 Gemini 去看图；另一次纯本地运行 5 步 69 秒完成改样式并提交，附件没有进提交。
6. **M6（已完成）：** AskUser、按步计价、审查者角色、@ 文件提及、PDF 页面渲染成图片、没有项目的 scratch chat，外加九个 bug 的修复。审查者的设计取舍见 [architecture.md](architecture.md) 第 7.7 节：论文靠提示词让模型自己安排"A 写 B 审"，我们做成开关加只读工具，因为本地小模型不会稳定地自己编排，而且审查者不该有能力偷偷改代码。
7. **M7（已完成）：** 子 agent（`Task` 工具）与上下文监控。子 agent 是**上下文过滤器**，不是并行：顺序执行，窗口 = 调用方剩余额度（上限 32K、下限 8K），过程存成 `parent_id` 指向调用方的子任务。取舍和实测数据见上面第 1.5 节，设计理由见 [architecture.md](architecture.md) 第 7.8 节。
   - **在线验证（2026-09-12）：** 同一个任务开/关子 agent 各跑一次，主 agent 上下文峰值 8,833 对 57,925；模型自己会判断——小调查一次都不派，"读完 24 个测试文件"一轮派了 4 个。
8. **M8（已完成）：** Skill —— 用户写的、按需打开的说明书，格式沿用 Claude Code 的 `SKILL.md`。设计取舍见 [architecture.md](architecture.md) 第 7.9 节：目录进提示词、正文进工具结果，因为前缀一改就要重算整段 KV。
   - **在线验证（2026-09-12）：** 项目里放一个规定 CHANGES.md 写法的 skill，任务只说"记录一下：我们加了子 agent 功能"，模型第一步就调 `Skill("changelog")`，4 步 15 秒，写出来的行完全符合格式。
9. **M9（已完成）：** 从界面登录其他机器。系统自带的 ssh + `SSH_ASKPASS` 把密码提问接到界面，一条连接带全部端口转发和那边的界面，断线重连，窗口切换到那台机器的 LocalAgent。设计和安全取舍见 [architecture.md](architecture.md) 第 7.10 节和不变量 28。
10. **以后：** `run_parallel`（本地加云端并行，单卡上并行不划算）、"建议的下一步"、接着某个历史任务继续、命令与任务的自动补全、云端花费上限。
11. **明确不做：** 让 agent 给每个项目自己写记忆。KISS Sorcar 实测这会降低质量：它的筛选规则已经排除了代码片段和符号名，但 agent 认为"持久"的东西（文件位置、接口约定、设计决定）下一个 commit 就可能失效，而且注入到之后每个任务里。本地 27B 比前沿模型更容易照着过期的记录走，120K 的小窗口也让每条过期记录都在和当前任务抢地方。需要长期生效的规则写进项目自己的 `CLAUDE.md` / `AGENTS.md`（`project_instructions` 每个任务都会读进系统提示词），那是用户写的，不会悄悄过期。

## 7. 待确认
- ~~上下文是否按槽位均分~~：Unified KV 已开启，两个槽位共用总额度。调度策略见第 1 节第 5 条。KV 占满时 LM Studio 具体怎么处理（报错，还是清掉空闲槽位的缓存）还没实测，M1 探针里加一个可选的压力测试。
- ~~思考能不能关~~：能，而且可以按请求关，见第 1 节。
- **模型选择：** 同尺寸的社区微调版和官方 instruct 版，工具调用的稳定性可能不一样。换模型前用 M1 的探针（`la models test`）对比一下。
