# LocalAgent 架构

**用途：** 改代码前先读这一页。上下文不够时，读它来代替通读全部源码。
**相关文档：** 设计与路线图见 [design.md](design.md)；KISS Sorcar 参考见 [kiss-sorcar-notes.md](kiss-sorcar-notes.md)。

## 1. 分层（对照 KISS Sorcar 的五层）

| 层 | 职责 | KISS 对应 | LocalAgent 实现 | 状态 |
|---|---|---|---|---|
| L1 会话 | 一段只追加的对话，跑 ReAct 循环直到 finish 或达到上限 | `KISSAgent` | `agent/loop.py` 的 `Agent._run_session` | ✅ M1 |
| L2 续跑 | 上下文快满时写交接笔记，开新会话继续 | `RelentlessAgent` | `Agent.run` + `agent/handoff.py` + `agent/prompts.py` | ✅ M2 |
| L3 工具 | 读写文件、命令、测试、搜索、联网、问用户、记计划 | `SorcarAgent` + `useful_tools` | `tools/*.py`：7 个工具加 WebSearch、WebFetch、AskUser、Plan、set_model、finish | ✅ M1/M2/M5/M10 |
| L4 会话记录 | chat、历史前 2 条加后 8 条、持久化 | `ChatSorcarAgent` + `persistence` | `agent/chat.py` 的 `ChatSession` + `store.py` | ✅ M2 |
| L5 隔离 | 每个 chat 一个 git 分支和 worktree，每个任务一个提交，由用户决定合并、保留还是丢弃 | `WorktreeSorcarAgent` | `worktree/manager.py` + `ChatSession._workspace` / `_commit` | ✅ M3 |
| 模型 | 注册表、客户端、并发、任务中途换模型 | `model_info` + 各厂商适配 | `models/*.py`；`Agent._switch_model` | ✅ M1/M5 |
| 附件 | 用户发来的图片和文档进入任务 | Read 把二进制当附件带上 | `attachments.py` + `tools/fs.py` 的 Read | ✅ M5 |
| 前端 | 命令行、界面 | VS Code / CLI / Web | `cli/*.py`；`server/*.py`（FastAPI + WebSocket）+ `frontend/`（React）+ pywebview 窗口 | ✅ M4/M5 |

**组合，不用继承。** `ChatSession` 持有 `Agent`；`Agent` 持有 client、tools、workspace。KISS 在 L3–L5 用的是继承，结果状态分散在各个子类里，读起来要来回跳。我们每一层只是一个类或一个函数，彼此靠参数传递。

## 2. 模块地图

```
src/localagent/
  config.py              程序自己的文件放哪儿：`LOCALAGENT_HOME`，否则是**检出目录下的 data/**（判断依据是上面有 pyproject.toml，
                         装成 wheel 时退回 ~/.localagent）。第一次用到时把旧的 ~/.localagent 整个 rename 过来，失败
                         （别的进程占着、或跨盘）就继续用旧的，不会一半在这边一半在那边；secrets.env 里的 API 密钥读写
  attachments.py         附件：内容寻址的存储、PDF 取文本、放进工作区的 .la-attachments/（unplace：删 chat 时拿走）、拼成消息里的图片块
  procutil.py            quiet()：所有子进程都用它，Windows 上加 CREATE_NO_WINDOW；new_group() / kill_tree()：要整棵杀掉的子进程（Bash、ssh）；
                         Background：一个任务的命令留在后台的东西（dev server、watcher），任务结束时统一停掉
                         （Windows 上进同一个 job object，其他平台按命令的进程组）
                         （桌面窗口用 pythonw 启动，没有控制台，否则每个 git 子进程都会弹一个黑窗）
  remote.py              其他机器：Remotes / Tunnel。一条系统自带的 ssh 带上这台机器的全部端口转发（只绑 127.0.0.1），
                         有 UI 命令时顺便在那边启动 LocalAgent 界面并取回地址；断线按退避重连；SSH_ASKPASS 把 ssh 的
                         提问（密码、新主机密钥）交给 la-askpass，再由界面回答。机器列表在 data/remotes.yaml。
                         连上有界面的机器后，每 5 秒比对本机 skill 的指纹，变了就用 skillsync 推过去。
                         port_check()：那边每个转发端口是谁在监听（读 /proc/net/tcp），连接时先查、之后每 2 秒查一次，
                         有别的用户就断开、不重连（不变量 41）。
                         run()：跑机器"有名字的命令"（remotes.yaml 的 commands，如 start/stop/status），
                         各起一条短 ssh，密码照样在界面里问；外面只能点名字，命令行本身只在 remotes.yaml 里
  askpass.py             la-askpass（GUI 入口，Windows 上不弹窗口，只用标准库）：ssh 需要回答时运行它，
                         它带着本次 ssh 进程专属的一次性口令去问本机服务，把用户输入的答案交回 ssh
  fsutil.py              remove_tree：能删除含只读文件（git 对象）和名叫 nul 的文件（Windows 上走 \\?\ 路径）的目录树，
                         并返回是否删干净；不要用 rmtree(ignore_errors=True)
  store.py               SQLite：chats / tasks / events（不写流式 delta）/ folders（界面打开过的文件夹，给"最近的项目"用）/
                         settings（界面跨启动记住的开关）；delete_chat：一个事务里删掉 chat 的事件、任务（含子 agent）和它自己；
                         orphaned_chats：文件夹已经不在的 chat（只列出来，删不删由人定，不变量 44）；
                         placed_attachments：一个 chat 往文件夹里放过哪些附件副本（从事件里读，不变量 18）
  skills.py              Skill：用户写的说明书（Claude Code 的 SKILL.md 格式）。
                         只有目录（名字 + 一行描述）进系统提示词，正文由 Skill(name) 工具按需返回，
                         所以前缀缓存不受影响；同名时 项目 > 用户目录 > synced-skills（从登录过来的电脑同步的）
  skillsync.py           把本机用户目录的 skill 打成 zip，经隧道交给已连接机器上的 LocalAgent（PUT /api/skills/synced），
                         那边解到 data/synced-skills/<本机名>/ 并整份替换；只从本机到机器，不回传
  models/
    types.py             ToolCall、Usage、ChatResponse、ModelError、ContextOverflowError、Cancelled
    registry.py          Provider / ModelSpec / RoleSpec / Registry（models.yaml）、effective_thinking、
                         is_local_url（私网/回环/Tailscale 视为本地）、switch_targets
    auto.py              角色写 auto 时：查本地服务当前加载了哪个模型，并采用它实际加载的上下文长度
    presets.py           常见服务的预设（LM Studio、Ollama、Gemini、OpenAI、Anthropic 兼容层、DeepSeek…）、
                         spec_from_discovery：从探测结果推出上下文长度、视觉、关思考参数
    sync.py              sync_local_models：本地 provider 的模型列表跟着服务器走
                         （新下载的加进来、删掉的去掉，30 秒内不重复问，连不上就保持原样）；
                         refreshed：写死名字的本地模型也采用它此刻实际加载的窗口；loaded_now：谁正加载着
    limiter.py           provider_slot()：每个 provider 的并发信号量
    thinking.py          ThinkingGuard：流式盯着思考，超过模型的 max_thinking_tokens（一个思考 chunk 算一个
                         token，文字多的 chunk 至少按 6 字符 1 token 算）或发现同一段重复出现就让客户端当场断流；
                         looping() 是判定本身
                         （只看思考，不看正文：正文重复往往是真的模板或样板代码，砍掉会毁掉一个文件）
    openai_compat.py     流式客户端：推理流、分片工具调用拼接、按请求关思考、文本工具调用兜底、
                         build_payload：模型的 extra_params（采样）合进每个请求，关思考时 thinking_off 再盖一层，
                         guard 叫停时中断（finish_reason="thinking"，断开连接也让服务器停下；这种回复没有 usage）、
                         adapt_messages（按模型改写消息：没有视觉就把图片换成说明；Gemini 的 thought signature）
    discovery.py         （连不上的服务只等 2 秒握手、15 秒内不再重试）/v1/models、LM Studio 的 /api/v0/models、Gemini 原生 /models（带上下文长度，不耗生成额度）；
                         loaded_models()：服务器当前加载了什么，缓存 10 秒
    probe.py             la models test 的各项探针（含看图探针 probe_vision 和只发 1–2 次请求的 run_quick_probes）
  tools/
    base.py              Tool（schema + 参数校验和类型容错）、ToolError、ToolOutput（带图片的结果）
    workspace.py         路径解析（含 /c/... 写法）、编码识别（UTF-8/GBK）、先读后改的哈希记录、forget_reads、
                         background（Background，和子 agent 共用一份：一个任务一份后台进程）
    fs.py                Read（带行号；PDF 自动取文本；图片交给能看图的模型）/ Write /
                         Edit（去掉误粘的行号前缀、容忍行尾空白、给出最相近的片段；拒绝改 PDF 和图片）
    shell.py             Bash：Git Bash 查找、guard_command（cd 越界 + 危险命令审批）、venv 环境、进程树终止、输出头尾截断、
                         null_redirects（命令里 CMD 式的 `2>nul` 改成 `/dev/null`，不变量 43）
    testing.py           RunTests：识别测试命令，输出以 [tests PASSED/FAILED · exit code N] 开头
    search.py            Grep / Glob（git ls-files 优先，遵守 .gitignore）
    ask.py               AskUser：只在有人能回答时注册（界面或交互式终端），每个任务最多问 5 次
    plan.py              Plan：模型的计划（有序的步骤，各带 pending / in_progress / completed，外加 notes 记设计决定）；
                         parse_plan 收下模型的各种写法（TodoWrite 的 content、字符串、JSON 字符串、清单行）；
                         只在能改文件的 agent 上注册（不变量 42）
    web.py               WebSearch（duckduckgo 简版网页 / searxng / brave / tavily，按 data/web.yaml）/
                         WebFetch（HTML 转文本：去脚本导航页脚、优先 <article>/<main>、链接保留成 [文字](网址)；
                         PDF 抽文字；每次 1 万字、用 start 分段，页面按工具集缓存）；WebSettings
  agent/
    conversation.py      只能追加的消息列表
    prompts.py           系统提示词（能改文件的 agent 用 PLAN_RULE，只读的用 OUTLINE_RULE）、交接请求、续跑说明、
                         插话模板、relevant_notes、capped_progress、
                         build_session_message（附件清单、续跑时的"Plan so far"也放在这条消息里）、set_model 工具描述
    handoff.py           session_digest：模型写不出笔记时的机械版摘要
    loop.py              Agent：L1 + L2 + 运行中插话 + 重试 + handoff_threshold + 换模型 + 每步的两种预算
                         （一步＝一次工具调用：被掐断后的重试不占步数；步数用完时也写一份笔记再退出）
                         （_step_budget：输出上限 max_output_tokens（会话第一步可用 max_output_tokens_first），
                         思考上限 max_thinking_tokens；一步没调工具时同一步再来一次：思考减半，
                         如果是输出上限被用完则输出也减半；第二次才关思考，第三次失败并说该改什么；
                         被上限截断 / 思考超限 / 思考打转三种情况的 notice 和给模型的提示语不同）
                         （set_model 工具、用户指定的 request_model、换过去的模型报错就退回原模型）
                         （Plan：最新计划记在 Agent 上并发 plan 事件；交接后放进新会话的第一条消息；
                         AgentResult.plan 带回给 ChatSession；思考被掐断时的提示语让它先用 Plan 记下已想定的部分）
    chat.py              ChatSession（L4）：format_history（前 2 条 + 后 8 条，每条截断；没做完的任务带上它最后的计划）+
                         持久化 + 审查/修复回合 +
                         任务收尾时停掉命令留在后台的进程（并发 notice）
    subtask.py           Task 工具：把一段工作交给子 agent，只拿回它的报告；预算不够就直接拒绝
    delegation.py        子 agent 怎么跑：自己的上下文（受预算限制）、事件存成子任务、调用方只看到报告
    review.py            审查者：用 reviewer 角色的模型读这次任务的 diff（只读工具），
                         用 finish(success=…) 表态；发现问题就把清单交回主模型修，修完一起提交
  worktree/
    git.py               git 命令的薄封装（不弹提示、core.quotepath=off）、status -z 解析、当前分支、各分支检出在哪个 worktree
    lock.py              每个仓库一把跨进程文件锁（Windows 用 msvcrt，其他系统用 fcntl）
    manager.py           ChatWorktree / prepare / commit_changes / merge / keep / discard / pending / repo_status / diff；垃圾文件规则
    commit_message.py    commit_message 角色写提交消息，失败时用任务首行；正文附上 Task/Result，合并提交附上 Tasks 列表
  cli/
    main.py              la init / models list|discover|test / run / chats / show / status / diff / merge / keep / discard / ui /
                         skills list|show|add|remove / web show|set|search|fetch / remote list|run
    render.py            终端渲染：按事件逐条打印
    console_input.py     唯一的 stdin 读取者：有待确认的问题就当回答，否则当作插话
    output.py            prepare_streams：pythonw 启动时 stdout/stderr 是 None，转到 data/ui.log；输出统一 UTF-8
  server/
    hub.py               从任意线程把事件推给所有 WebSocket 客户端；给中途连上的客户端保留当前任务的事件记录，其中流式片段会合并
    runner.py            TaskRunner：同一时间只跑一个任务，放在工作线程里；支持插话、停止、命令审批（Approvals）；任务结束后推送 repo 状态；
                         记下任务的开始时间（state 和 task_started 都带上），界面据此显示计时，刷新页面也不会从零开始
    app.py               create_app：REST 接口（state/models/repo/fs/chats/changes/merge/keep/discard/
                         attachments 上传和读取）+ /ws + 静态页面；全部需要 token
    models_api.py        注册表管理接口：provider（含密钥写入 secrets.env）、探测模型、增删模型、角色、快速测试
    remote_api.py        机器接口：增删改、连接/断开、回答 ssh 的提问；/api/askpass 不用界面 token，只认一次性口令、只接受本机来源
    files_api.py         资源管理器和编辑区的数据（只读）：一层层展开的目录树，每项带 git 状态（M/A/D/U）、是否被忽略、已删除的文件也列出；
                         整个根的改动清单；单个文件的内容（文本、图片、二进制）、它的 diff，以及它在基准时的内容 base_text
                         （左右对比 diff 的左边；git.blob 按字节读出再识别编码）。根有两种：用户的检出（对 HEAD），
                         或某个 chat 的 worktree（对它的 baseline，提交了的和没提交的都算）；路径越出根一律拒绝
    desktop.py           la ui：在本机随机端口启动 uvicorn，打开 pywebview 窗口（DesktopBridge.pick_folder 调原生选文件夹对话框），或用 --browser 在浏览器打开；
                         不带 -w 时地址里不带 dir，页面打开服务器记下的上一个文件夹；text_select=True（pywebview 默认禁止选中文字，页面上什么都复制不了）；
                         窗口图标用 assets/localagent.ico
    static/              frontend 的构建产物（已提交，运行时不需要 Node）
frontend/                React + TypeScript + Vite 源码；npm run build 输出到 server/static
  src/api.ts             REST 和 WebSocket 客户端（token 从 URL 的 #token= 读取；断线自动重连；每一帧批量处理事件）
  src/timeline.ts        buildTimeline：把事件流（实时或从数据库回放）转成步骤、工具卡片、交接、换模型、结果等界面条目
  src/imageTools.ts      图片处理：解码（必须 decode() 后再画，否则只画出一部分）、超过 1600px 缩小、导出 PNG
  src/App.tsx            VS Code 式的工作台：标题栏 / 活动栏 + 侧栏 | 对话 | 编辑区 / 状态栏，外加快速输入框和各个对话框。
                         状态：文件夹（启动时没指定就用服务器记下的上一个）、repo 状态（每 10 秒刷新一次）、chat 列表、当前任务的实时事件、
                         编辑区的标签页（只在这个窗口里，不跨启动）、审批、附件查看；快捷键 Ctrl+P / Ctrl+Shift+P（F1）/ Ctrl+B / Ctrl+Shift+E / Ctrl+Shift+G。
                         要跨启动保存的东西放服务器的 settings（remember()）：桌面窗口每次换端口，localStorage 跟着源走，下次启动读不回来。
                         布局也在里面：sidebar_view / sidebar_hidden / sidebar_width / editor_width / diff_side_by_side / theme
  src/editorTabs.ts      编辑区标签页：file（文件）/ diff（对基准）/ changes（一个 chat 分支的全部改动）；单击是预览标签，下一次单击替换它；
                         treePath：把 agent 在工具调用里写的路径（worktree 里的绝对路径、检出里的绝对路径、相对路径）换成"哪棵树 + 树里的路径"
  src/monaco.ts          Monaco 的精简入口（按需加载，单独一块）：编辑器、只读用得上的功能、各语言的语法着色、diff 用的 worker；
                         不带 TypeScript / JSON / CSS / HTML 语言服务（JSON 借 JavaScript 的着色）；la-dark / la-light 两个主题
  src/fuzzy.ts           快速打开和命令面板的模糊匹配（字符按顺序出现即可，连续、词首、驼峰加分；路径先匹配文件名）
  src/fileIcons.ts       文件类型 → Codicon 图标和颜色 · src/theme.ts：颜色主题（跟随系统 / 深色 / 浅色，写在 <html data-theme>）
  src/components/        TitleBar（项目菜单、命令中心、侧栏和编辑区的开关）/ ProjectMenu（最近的项目；有文件夹已不在的 chat 时多一项"清理"，打开 CleanupDialog 按文件夹列出、勾选后删）/
                         ActivityBar（视图图标、改动数徽标、齿轮菜单；ViewPane / ViewSection：侧栏视图的标题和可折叠分节）/
                         Sidebar（ChatsView：会话列表）/ FileTree（资源管理器：目录树、缩进线、按 git 状态上色；单击预览、双击固定）/
                         ScmView（源代码管理：选中 chat 的分支，合并 / 保留 / 丢弃、改过的文件、提交；检出里没提交的改动）/
                         EditorGroup（编辑区：标签栏、标签的操作按钮、面包屑、各种标签的内容）/ CodeEditor（Monaco：FileEditor 在行号旁标出改动，
                         DiffEditorView 左右或行内对比；换标签时记住滚动位置）/ StatusBar（机器、分支、运行计时；步数、审查、云端、思考、模型）/
                         QuickInput（快速打开、命令面板和各个选择列表：模型、机器、最近的文件夹、颜色主题，以及输入步数）/ Sash（拖动调宽度，双击恢复默认）/
                         Codicon（VS Code 的图标字体）/ ModelPicker（ModelDot：已加载的模型是呼吸的绿点，没加载的是空心圆）/ Menu（弹出菜单的关闭逻辑）/
                         CopyButton（先用 Clipboard API，被拒绝就退回 execCommand）/ ChatHeader（对话区顶上：chat 分支，或新 chat 的分支选项）/
                         ContextGauge（上下文用量条、交接位置、当前速度）/ Mentions（@ 文件补全）/ QuestionDialog /
                         Transcript（只有停在底部时才跟随新输出，往上滚就停住；右下角：chat 顶部、最新一轮开头、回到底部并跟随）/
                         ToolCard（Plan 调用画成清单：✓ / ▸ / ○，是记录那一刻的样子，只写"N steps"不算完成数；notes 点开看；
                         Read / Write / Edit 的卡片经 OpenFileContext 在编辑区打开文件或它的 diff）/ Composer（运行 / 插话 / 停止 / 附件）/ Attachments（缩略图和文档卡片）/
                         Annotator（在截图上画框、箭头、涂画）/ ModelsDialog（provider、密钥、模型、角色、测试）/
                         DiffView（整段 unified diff，一个文件一块；parse 也给源代码管理列文件）/ FolderPicker / ApprovalDialog /
                         Machines（机器管理、ssh 登录提问框；切换机器在状态栏左下角，另一台机器的页面上那里显示机器名，可以回到本机）
tests/                   离线测试（scripted_server.py 是一个真实 HTTP 服务，按脚本返回流式响应）+ test_live.py
scripts/demo_fix_bug.py  验收脚本（--context-budget 可以人为缩小窗口来强制交接）
scripts/setup.ps1 · setup.sh  新电脑上的安装：找 Python 3.12+、建 .venv、装依赖、（Windows）建快捷方式
scripts/make_icon.py     生成 assets/localagent.ico 和 frontend/public/icon.png（网页的 favicon 和顶栏 logo；纯 Python 画的，两个文件都已提交）
scripts/gpu-server/      不属于本程序，是拷到 GPU 服务器上用的：llama-swap 配置 + 启动脚本（挑空卡、防孤儿进程）；
                         setup-searxng.sh / run-searxng.sh：给 WebSearch 用的私有 SearXNG（源码包 + uv，只听 127.0.0.1:18888）；
                         la-services.sh：一条命令管三个 tmux 会话（la-swap / la-search / la-ui）的 status|start|stop|restart。
                         端口是谁的按 uid 判断（不变量 41）；stop 先 TERM 会话里的全部进程再关会话。
                         程序照常把它当成一个 OpenAI 兼容的 provider（见 README、design.md §1.6）
setup.cmd · LocalAgent.cmd    双击安装 / 双击开窗口；快捷方式用 pythonw 启动 la ui，所以没有控制台窗口
                         （没有控制台时 stdout 是 None，cli/output.py 的 prepare_streams 会把输出转到 data/ui.log）
examples/buggy_stats/    验收用的示例项目，含两个 bug
```

## 3. 一个任务的数据流

```
la run -w DIR "task"
 └─ ChatSession.run(task)                                   L4
     ├─ store.new_chat / latest_chat；history = format_history(store.tasks(chat))
     ├─ worktree.prepare(DIR, chat_id, base)                L5
     │    建立 <repo>/.la-worktrees/<chat>，分支 la/<chat>，从 base 分出（默认是当前分支）
     │    如果你有未提交的改动，复制进去作为 baseline 提交；再次续接这个 chat 时复用；
     │    被 keep 过的 chat 会从分支重新建出 worktree；不是 git 仓库时直接在 DIR 里干活
     ├─ Workspace(worktree 目录, venv 查找还包括 DIR 和仓库根目录, 禁止命令提及主检出目录)
     ├─ attachments.place(...)：把这个 chat 的附件复制进 <工作区>/.la-attachments/
     │    （这次任务的附件带上图片数据和小文档正文；之前任务的只在历史里列出路径）
     ├─ store.add_task（附件元数据存进 tasks.attachments）；把事件发给 store.record_event 和终端渲染器
     ├─ Agent.run(task, chat_context=history, attachments=placed)   L2
         for session in 1..max_sessions:
           workspace.forget_reads()
           opening = build_session_message(task, history, relevant_notes(notes), continuation, 附件清单)
           Conversation(system_prompt) + opening（有图片时是 text + image_url 的内容块）
           _run_session(conv)                                L1
             每步：插话入队 → client.chat(完整 messages, 工具 schema, thinking)
                   → 追加 assistant 消息 → 执行工具 → 追加工具结果（最后一条带状态行）
             结束方式：finish → 完成
                       上下文 ≥ handoff_threshold → 在同一段对话里要交接笔记（不改工具、关思考）
                       上下文溢出 → session_digest
                       停止 / 出错 / 步数用完
           交接 → notes.append((session, text, source))，进入下一个会话
     ├─ commit_changes(worktree)：暂存时排除垃圾文件，由模型写提交消息 → 发出 task_committed 事件
     └─ store.finish_task
之后由用户操作：la merge → 在 base 分支所在的检出里执行（先 stash 已跟踪文件的改动，
                   cherry-pick --no-commit baseline..la/<chat>，提交，再 stash pop）；
                   如果 base 没有被检出，就在一个临时 worktree 里做 → 删除分支和 worktree
               la keep → 提交剩余改动，删掉 worktree 目录，保留分支
               la discard → 删掉分支和 worktree
```

## 4. 不变量（改代码时必须保持）

1. **对话只追加。** 只能通过 `Conversation.append` 加消息，历史消息一律不改。
2. **同一工作目录下，系统提示词在各个会话、各个任务之间字节一致。** 每个任务不同的内容（chat 历史、交接笔记）只放在第一条 user 消息里。**同一个会话内工具列表不变**；请求交接笔记时也保留工具，因为 chat 模板会把工具渲染进前缀。
   - 实测依据：追加一轮时首 token 0.8 秒，改动开头一行则要 48 秒。
3. **状态行** `[step x/y · context a/b tokens]` 只加在一轮中最后一条工具结果的末尾；模型记过计划后再加一段 `· plan c/d done`（第 42 条）。
4. **每个新会话都调用 `forget_reads()`。** 模型已经不记得文件内容，Edit 前必须重新 Read。
5. **所有 shell 命令都经过 `guard_command`。** Bash 和 RunTests 的自定义命令都走这里：拦截 cd 离开工作区的命令，危险命令要审批。
6. **子进程环境：** LocalAgent 自己的 venv 从 PATH 里去掉；如果目标项目有 `.venv`，放在 PATH 最前面；统一设成 UTF-8。
7. **每个 provider 同时进行的请求数不超过 `max_concurrency`**，由 `provider_slot` 保证。
8. **流式 delta 不写数据库**（`store.STREAMING_EVENTS`）。
9. **工具报错一律用 `ToolError` 返回给模型**，不抛到循环外面。工具自身有 bug 时也会被兜住，转成错误结果。
10. **隔离模式下，任务本身永远不改动用户的检出。**
    - 只能写 worktree 里的文件；命令里只要提到主检出目录的路径就会被拒绝（`names_protected_path`）。
    - 只有用户主动合并才会改动用户的分支。
    - 合并从不切换用户的分支：base 分支没有被检出时，改用临时 worktree。
    - 合并只 stash 已跟踪文件的改动，不 stash 未跟踪文件（未被忽略的 `.venv` 可能很大）。
11. **worktree 的状态只存在 git 里**：分支名 `la/<chat>`，以及 `branch.la/<chat>.la-{base,start,baseline,subdir,chat}` 这几个配置项。数据库里的 `chats.branch` 只是给显示用的。
12. **垃圾文件规则**（`JUNK_*`）同时用于"复制用户的未提交改动"和"提交"：各种缓存、`node_modules`、根目录的 `.venv` / `venv` / `tmp`，以及任何一层名叫 `nul` 的文件（不变量 43）。
13. **修改分支的操作都要先拿到这个仓库的 `file_lock`**：prepare、merge、keep、discard。
14. **服务端安全**
    - 默认只监听 `127.0.0.1`。
    - 所有 `/api` 请求和 `/ws` 都要 token。token 放在 URL 的 `#` 片段里，浏览器不会把它发给服务器，也不会进日志。
    - 服务端可以通过 agent 执行命令，所以不要加不需要 token 的接口。
15. **界面同一时间只运行一个任务**（`TaskRunner`）。命令行进程可以同时运行，git 和数据库操作都有锁保护。
16. **写进数据库的事件必须足够回放整个任务。** 流式片段不写库，所以 agent 会在每一步额外发出 `assistant` 事件，里面是完整的文本和推理。
17. **密钥只存在 `data/secrets.env`。** `models.yaml` 里只写变量名（`api_key_env`）；读密钥时**不写进 `os.environ`**，所以 agent 执行的命令不会继承到密钥；接口一律只返回 `has_key`，不返回密钥本身。
18. **附件不进项目历史。** 附件放在工作区的 `.la-attachments/`，该目录里有一个 `*` 的 `.gitignore`（自己忽略自己），同时也在垃圾规则里，所以既不会出现在 `git status`，也不会被提交。原件存在 `data/attachments/<id>/`，按内容寻址。**删 chat 时把它放进文件夹的副本拿走**：每个任务的 `task_start`、`steer` 事件里记着放了哪些路径（PDF 旁边还有一个 `.txt`），同一文件夹里剩下的 chat 没用到的就删，目录里只剩 `.gitignore` 时整个目录删掉，项目回到原样。只有直接在文件夹里干活的 chat（不是 git 仓库、scratch）会把副本留在项目里；在分支上的 chat 副本在它的 worktree 里，跟着 worktree 走。删之前按文件夹比对，所以同一个附件被两个 chat 共用时，删一个不会拿走另一个的。
19. **对话本身与模型无关；适配发生在发请求的时候。** `adapt_messages` 按当前模型改写要发出去的消息（不能看图的模型把图片换成一句说明；Gemini 需要 thought signature，别的服务要去掉 `extra_content`），**不修改 `Conversation`**，所以换模型不会破坏"只追加"。
20. **角色可以不写死模型名。** 角色的 model 写 `auto`（或 `auto:<provider>`）表示"用本地服务当前加载的那个模型"，每次解析时现查，并采用它**实际加载**的上下文长度和视觉能力；查不到就报清楚的错（哪个 provider 连不上、或者根本没加载模型），不会偷偷换一个。单卡机器上这一条很重要：写死名字会让下一个任务把服务器上加载的模型顶掉。
21. **子 agent 是上下文过滤器，不是并行。** 它在调用方等待时运行（同一时刻只有一个请求在飞），窗口按"调用方窗口减去已用再减去保留额度"算，上限默认 32K（模型可以用 `max_subtask_context` 自己设，比如 KV 池比单个对话大得多的服务器） —— 两段对话要同时待在服务器的共享 KV 里，挤掉调用方的前缀比子任务省下的贵得多。预算不足就拒绝，并让调用方自己做。子 agent 不能再开子 agent。**并行是按模型开的例外：** `max_parallel_subtasks` 大于 1 的模型（A100 上的 Qwen 设为 3，比服务器的 4 个槽位少一个，调用方空闲的槽位和它缓存的对话不会被分给子 agent），同一轮里的**只读** Task 调用同时跑，预算按同时跑的个数平分，结果按调用顺序放回对话；可以写文件的子 agent 永远单独跑；连两份最小预算都放不下时照常排队。
22. **审查者不能改代码。** 它只拿到 Read / Grep / Glob / RunTests（联网开着时再加 WebSearch / WebFetch，它们只读网页、不碰磁盘），而且**只读模式下的 RunTests 没有 `command` 参数**——否则它就是一个 shell（实测中审查者正是用 `RunTests(command=...)` 跑 git、mkdir 去重建改动前的状态）。只读的子 agent 同理。要改的东西交回主模型，因此"审查"永远不会变成"第二个作者偷偷改了东西"。
23. **子进程不弹窗口。** 所有 `subprocess` 调用都走 `procutil.quiet()`。有测试扫描源码，新增的调用点忘了就会失败。
24. **本地模型列表不是配置，是服务器的现状。** 本地 provider 的模型列表在展示和开跑之前重新拉一遍（`sync_local_models`，每个 provider 30 秒内不重复问，连不上就保持原样，被角色指名的模型即使服务器上没有了也保留）。加载状态和加载时的上下文长度**永远现查**（`loaded_now` / `refreshed`），不写进 `models.yaml` —— 写进去就会过期。云端 provider 不自动拉：目录很长，而且每次要发请求。
25. **skill 的正文永远不进提示词。** 提示词里只有目录（每个 skill 一行），正文走 `Skill(name)` 工具作为工具结果返回。前缀一改就要重算整段 KV，而正文动辄几千 token；目录只在用户增删 skill 时才变，所以同一个工作目录里的任务一直命中缓存。
26. **换模型要能退回来。** `set_model` 只在注册表里勾了 `switchable` 的模型之间切换（云端还要 `allow_cloud`）；上下文装不下就拒绝；换过去的模型请求失败（密钥失效、没额度、连不上）就自动退回任务开始时的模型并继续，不让任务直接失败。
27. **帮手跟着任务所在的机器走。** 子 agent、审查者、写提交消息的这三个角色如果写的是 `auto` 或者没写（没写时只借用 main 的思考设置，不借它的模型），而任务跑在一个本地模型上，它们就直接用任务正在用的那个模型（`Registry.helper`），不再各自去解析 `auto`。原因：有两台本地服务时（家里的 LM Studio 和隧道后面的 GPU 服务器），重新解析 `auto` 会落到先应答的那台——换了一台机器，或者只有任务那台在线时直接失败；而任务正在用的模型一定是现成可用的。任务跑在云端时保持 `auto`，免得便宜的帮手活开始花钱；角色写了具体模型名或 `auto:<provider>` 时照写的来。
28. **ssh 登录不降低安全性。** 用系统自带的 ssh 和用户自己的 `~/.ssh/config`，**永远不关主机密钥校验**（不传 `StrictHostKeyChecking=no`，也不用一次性的 known_hosts）：主机密钥变了 ssh 直接退出，根本走不到输密码那一步。密码只在内存里从密码框转交给等着的 ssh 进程，**不写数据库、不写日志、不进事件 backlog**（ssh 相关事件用 `publish(remember=False)`）。`/api/askpass` 只认本次 ssh 进程环境变量里的一次性口令、只接受本机来源，本机别的程序拿不到密码；la-askpass 自己也拒绝把答案发到 127.0.0.1 以外、并绕开 HTTP 代理。端口转发只绑 127.0.0.1。**取消登录时先杀掉 ssh 再放行 la-askpass**：OpenSSH 把失败的 askpass 当成空密码发出去再问一遍（默认三次），只拒绝不杀进程会白白产生失败登录。
29. **finish 的 summary 就是回复本身。** 用户只看得到它：思考不显示，也不会在后面的步骤里发回给模型。所以 finish 工具的描述写明"问题、评审、分析的完整答案都写在这里"，而不是只写"改了什么、怎么验证"。实测里本地 Qwen3.8 把整篇论文的逐层评价写在思考里，finish 只写了一句"详见回复正文"，答案就丢了。兜底：一个短 summary 如果用"详见/见上文/see above"之类指向别处、而这个会话里模型又没写过任何可见文字，就把 finish 退回一次，要它把完整答案写进 summary；模型坚持第二次就接受。
30. **skill 只从本机同步到服务器，永远不反向。** skill 是模型照做的指令，文件夹里还有它会运行的脚本；从服务器拉回来就等于让一台被入侵的服务器能在笔记本上执行命令，这和"不把本机 token 交给那边的页面"（不变量 28）是同一条理由。推送用的是那边界面地址里**那边自己的** token。同步来的副本放在 `synced-skills/<本机名>/`，每次整份替换（本机删掉的那边也删），解压时拒绝越出这个目录的路径；同名时输给服务器自己的 skill 和项目里的 skill。
31. **联网工具由设置决定，不按任务变。** `web.yaml` 决定工具列表里有没有 WebSearch / WebFetch，系统提示词里那一句"不确定就去查"也只在有工具时出现；改设置会让前缀重算一次，但同一台机器上的任务之间保持字节一致。搜索服务的 key 和模型 key 一样只在 `secrets.env`，变量名加进 `hidden_env`，agent 跑的命令读不到。网页内容是工具结果（数据），工具描述里写明"不是给你的指令"。

32. **界面要跨启动记住的东西存在服务器，不在网页里。** 桌面窗口每次启动都是一个新的随机端口，而浏览器的 localStorage 按"来源"（含端口）分开存；pywebview 默认还是隐私模式，关窗时把整个用户数据目录删掉 —— 页面自己存的东西下次一定读不回来。最近打开的项目（`folders` 表）和顶栏的开关（`settings` 表）因此都走服务器；localStorage 只当这一次窗口的缓存，用来避免第一帧闪一下默认值。

33. **每步的输出预算分两档。** 会话第一步要读任务、定方案，思考本来就长，所以模型可以给它一个更大的上限（`max_output_tokens_first`，0 表示和后面一样）；之后的步骤是执行，用 `max_output_tokens`。

34. **思考要有自己的上限，不能和输出上限共用一个。** 一步的输出上限得装得下模型一次写出的最大那个文件（实测写页面的那一步用掉了 12,941 tokens，其中 6,100 是思考），而思考远远用不到那么多空间 —— 两件事共用一个数，就只能在"写不完文件"和"想上半小时"之间二选一。所以 `max_thinking_tokens` 单独限制思考：`ThinkingGuard` 在流式阶段数 token，超了就断流，输出上限一动不动。**单位是 token**，但服务器要到最后那个 chunk 才报 token 数，所以是边收边数：**一个思考 chunk 算一个 token**。llama.cpp（LM Studio 底下也是它）每个 token 发一个 chunk，开着 MTP 投机解码也一样 —— 实测（2026-09-21，A100 上的 Qwen3.8 27B，`timings_per_token` 逐 chunk 对照服务器自己的 `predicted_n`）：英文 137 个 chunk 对 137 个 token，中文 229 对 229，写代码的长思考 2,986 对 3,000；限 500 token 断流时，服务器自己数到的是 500 和 502。服务器若一个 chunk 塞好几个 token（云端 API），就按字数兜底：至少 6 个字符算 1 token（实测英文思考 2.6–4.6 字符/token，中文 1.15，都低于 6，所以兜底不会在逐 token 的服务器上多算；最初定的 5 离英文散文式思考的 4.6 太近）。原来用的是字符数，理由是"流里只有字符"；可同一个上限对中文宽了三倍：48,000 字符在英文是约 13K tokens，在中文是约 42K。定这个数要照着"有产出的那一步最长想了多少"来留余量：实测最长的一次有产出的思考是 22,424 字符（约 6.4K tokens），24,000 字符的上限差点把它砍掉，所以 27B 上给的是 48,000 字符，换成 token 是 13,700（旧的 `max_thinking_chars` 读进来时按 3.5 字符 = 1 token 换算，取整到百）。

35. **打转要当场停，但它只是保险丝，不是闸门。** 同一段 400 字符在最近 4 万字符里再出现一次（`looping`，times=2）就断流。它比思考上限更早发现"接下来说的都是重复的"，但**发现不了刚开始的打转**：重复要先完整走完一个周期才看得见，实测那次周期有 10,800 字符，任何逐字比对的办法都不可能在 22,600 字符之前发现。所以闸门是上一条的思考上限，这条只是把已经确定白烧的那几分钟省下来。**只看思考，不看正文**：正文里重复的往往是模板、表格、成批相似的函数，砍掉等于毁掉一个文件。断流的回复没有 usage（服务器最后那个 chunk 没发出来），所以这一步不计 token，`_threshold` 也不会用它来算。

36. **模型不调工具时，降级要一级一级来。** 一步没有工具调用（被输出上限截断、思考超限、或者思考打转），先说清楚是哪一种（notice 事件），然后**同一步再来一次，思考上限减半**（没配上限的模型就按它这次实际想了多少的一半来，所以每一级都比上一级紧）；只有输出上限被用完时才连输出预算一起减半 —— 思考超限时输出预算不动，那不是问题所在。**第二次失败才关掉思考**：没有思考的一步质量明显更差，所以它是最后一档；第三次失败，并指出该调小思考上限还是换一个不那么啰嗦的模型。实测依据：一个 27B 的社区微调在 16K 输出上限下连续 6 步被截断，每步约 8 分钟，界面上只显示"thought 56,370 characters"，49 分钟一无所获；把上限提到 60K 只是让它转得更久 —— 真正的解法是上面那条思考上限。

37. **下一项工具操作确定，就结束本轮规划。** 思考不进对话（不变量 29），计划和决策要记进 Plan（第 42 条；只读的 agent 没有 Plan，仍是写进正文）。提示词要求只想清下一次工具调用和参数，立刻执行；多文件任务先记计划，再迈出第一步。新项目先建可运行骨架、检查，再逐步加功能。思考额度仍按第 36 条重试，调用工具后恢复配置额度；不跨步骤累积缩减，也不在后续三个工具回复中强制关闭思考，避免一次循环影响后续正常编码。

38. **任务结束要收拾现场，包括进程。** 模型会为了验证启动 dev server、watcher、无头浏览器，然后忘记关掉（提示词里要求它关，但它会忘）。命令一返回 bash 就退出了，`&`/`nohup` 留下的东西没人管，端口一直被占着。`Workspace.background`（`procutil.Background`）收下每条命令的进程组 / job object，任务结束时（`ChatSession.run` 的 finally，主 agent 和子 agent 共用一份）统一停掉，停到东西了就发一条 notice。**不能在命令返回时就停**：下一步要 curl 上一步起的服务，那正是它存在的理由。

39. **采样参数跟着模型走，不发就是服务器说了算 —— 而服务器可能是贪心解码。** 我们原来一个采样参数都不发。实测（2026-09-19，LM Studio 上的一个 Qwen3.8 27B 社区微调）：同一个请求发四遍，回复**逐字相同**，和显式 `temperature=0` 一模一样；同一个提示加上 `temperature=0.8 top_k=40` 就变了。贪心解码正是思考模型无限重复的经典成因 —— 那次 10,800 字符一个周期的逐字循环，在采样解码下几乎不可能发生，在贪心下则是必然：状态一旦回到原处，后面每个 token 都会一模一样。Qwen3.8 官方给的是两档：思考 `temperature=1.0, top_p=0.95, top_k=20, min_p=0.0`，不思考 `temperature=0.7, top_p=0.80, presence_penalty=1.5`。正好对上已有的两个字典：**思考那档放 `extra_params`（每个请求都发），不思考的差异放 `thinking_off`（关思考时再覆盖）**。字典里没有的键就不发，所以"用服务器默认"不需要发明 -1 / -inf 这种哨兵值 —— 而 0 对 `temperature` 和 `min_p` 都是有意义的取值，用哨兵反而会出错。不做成 temperature / top_k 这样的独立字段，是因为 `top_k`、`min_p`、`repetition_penalty` 都不在 OpenAI 规范里，各家服务的名字也不一样（`repeat_penalty` / `repetition_penalty`），而云端模型发过去会被拒；自由字典既能表达这些，也能保持"没写就不发"。注意 LM Studio 对**不认识的键是静默忽略**的（实测），写错名字不会报错。

40. **一步就是一次工具调用；步数上限由用户定，用完了要留下笔记。** 思考被掐断后的重试是**同一步再来一次**，不占步数也不发新的 `step_start` —— 否则界面上的"第 N 步"和模型看到的 `[step N/100]` 都在数它没做成的事。上限本身在顶栏里可调（`max_steps`，存在 settings 表里，跟着 run 请求走），因为"一个项目要几步"只有用户知道：实测那次 100 步用完时，95 步都有工具调用（Write 34、Edit 24、Bash 26、Read 22），不是空转，是活确实没干完。**步数用完时和交接一样写一份笔记**（`_handoff_notes`，notice 文案不同）并放进 summary，这样同一个 chat 里下一条消息能接着干 —— 否则用户只会看到"Stopped after reaching the step limit (100)"，前面几小时的上下文全丢。

41. **转发只通到用户自己的服务。** 共享服务器上谁都能在 `127.0.0.1` 上监听；用户的服务没在跑时（重启后、停掉后、模型卸载后），那个端口谁先占就是谁的。转发过去，对方就拿到全部请求（代码、对话、API key），他的回复还能指挥本机的工具 —— 这是 ssh 隧道里唯一一条"反向"的路。所以 ssh 会话在 UI 命令**之前**先查一遍每个转发端口的监听者，之后每 `WATCH_EVERY`（2 秒）再查一次（`remote.port_check`：读 `/proc/net/tcp{,6}` 的 uid 列，任何地址都算，因为监听 0.0.0.0 的也收得到转发）。有别的 uid 就立刻杀掉 ssh、**不重连**（重连会先打开转发、后跑检查），`refused` 置真，界面报出端口和 uid —— 页面当时可能正显示着那台机器的界面，收不到事件，所以本机页面加载时也要报。没人监听不算问题。在"连上"之前不从本机连转发端口，检查没通过就没有任何字节发过去。剩下的窗口：ssh 登录成功到第一次检查之间（转发已经打开；2026-09-21 经跳板机到 GPU 服务器实测 0.2 秒），以及两次检查之间。实测同一天：服务器上 uid 998 监听的 12345 端口，1.3 秒内拒绝；自己的 18080 和 8765 正常连上并持续监视。GPU 服务器上 llama-swap 按需拉起的 llama-server 端口同理，`start-llm.sh` 启动前确认端口空着、启动后确认第一个监听者是自己。`la-services.sh` 也一样：`status` 不用"端口通不通"判断服务在不在（别人占着也通），改成看监听者的 uid，是别人就报出来并以 1 退出；`start` 遇到已被占的端口不启动。

42. **计划要放在对话里，不能只在思考里。** 思考不会发回给模型（不变量 29），所以第一步在思考里定下的方案，第二步就看不见了。翻 2026-09-21 之前的任务记录：5 次"从零搭一个 Three.js 体素场景"，第一步都在思考里把整个项目设计了一遍（24K–103K 字符，其中一次在思考里写了 246 行代码），耗时 5–17 分钟，之后第一个工具调用是 `ls -la && node -v`，或者根本没调就撞了上限；**接下来的步骤又从头设计**：其中一次第 3–6 步每步 45–49K 字符，7 步只写出一个文件；一共想了 24 万字符。小任务不受影响（第一步中位数 8 秒）。提示词里早就写着"多文件先写个提纲、思考每步之后会丢"，模型照样不写。所以改成一个**工具**：
    - **`Plan(steps, notes)`**：步骤按顺序、各带状态，notes 放后面步骤要依赖的决定（文件布局、命名、算法、参数），**不放代码**。调用参数是对话的一部分，后面每一步都看得到；这也和"只想到下一次工具调用"（第 37 条）不冲突 —— 大任务的下一次工具调用就是记计划。
    - **跨会话、跨任务带着走**：交接后，新会话的第一条消息里有"Plan so far"（笔记之后、续跑说明之前）；任务没做完就结束（出错、停止、步数用完、被中断）时，计划存进 `tasks.plan`，同一个 chat 的下一个任务在历史里看到"Plan N when it stopped" —— 记录里那次"再试一次，刚刚报错了"又从头想了 10 万字符。做完的任务不带（没用了，还占上下文）。
    - **只给能改文件的 agent**：审查者和只读子 agent 没有 Plan，提示词里还是原来那条"写个提纲"，所以它们的前缀不变。
    - **思考被掐断时**，提示语从"写到文件里"改成"用 Plan 记下已经想定的部分"：被掐断的思考整段丢掉，只有进了工具调用的才留得下来。
    - **不设审批**（不像 Copilot 的 Plan 模式或 Claude Code 的 plan mode 那样等人点头）：这里的任务常常是放着跑的，计划显示在界面上，要改就插话。
    - **计划里的状态靠不住，就别当真。** 实测模型记完计划就再也不碰它（下面的 20 步实测，三次都只调了一次 Plan）。试过在状态行里加计数（`· plan 2/7 done`，照 KISS 论文 §6.10 用"已访问网站 N/10"计数头的思路）：又跑了 20 步，第 2 步记下 7 步计划后 18 步一次没更新，计数一直是 0/7 —— 没用。所以不再指望状态：带进新会话的"Plan so far"和带进下一个任务的"Plan N when it stopped"都写明"状态可能过时，以交接笔记和文件为准"；界面上的计划卡片只写"N steps"、不写"n done"，清单保持记录那一刻的样子（它本来就放在记录它的那一步里）。计数仍留在状态行里：只多几个 token，模型偶尔标了也有用。计划真正起作用的是那份设计和步骤划分，不是勾。
    - **实测**（2026-09-21，A100 上的 Qwen3.8 27B，同一个体素任务，改之前的代码和改之后的代码各跑两次、各 6 步）：思考被截断，旧代码每个 run 4 次、新代码 1 次；被截断白烧的思考 144K–146K 字符对 44K–48K；6 步用时 27–32 分钟对 12–17 分钟。新代码第 3 步记下计划后，写文件的每一步思考都不到 1K 字符。**第一步不受影响**：四次都想满 13,700 token 被截断，重试时都只调了 `ls` —— Plan 省下的是之后的重想，不是第一步。两次计划的 notes 都是空的，设计决定写进了步骤本身。再跑 20 步（旧 1 次、新 2 次）：截断 8 次对 1–2 次，白烧的思考 267K 对 24K–82K 字符，20 步用时 53 分钟对 15–37 分钟；新代码记下计划之后写复杂文件时也没有从头重想（B2 之后每步不超过 2K 字符）。**但模型从不更新计划**：三次都只调了一次 Plan，状态一直是全部未完成，所以交接和"再试一次"带过去的进度是过时的。

43. **命令里的 `nul` 是 bash 的文件名，不是空设备。** 模型习惯写 CMD 的 `2>nul`、`>NUL 2>&1`，而 Bash 跑在 Git Bash 里，结果是在项目里留下一个名叫 `nul` 的普通文件。Windows 上这个名字指的是设备，于是：资源管理器和回收站删不掉它；Python 的 `rmtree` 也删不掉，丢弃 chat 时整个 worktree 目录留在原地；git for Windows 读它读到的是空设备（`short read while indexing nul`），`git add -A` 整个失败，任务的提交跟着失败。2026-09-21 清理测试项目时撞上的，来源是模型写的 `where msedge 2>nul`。三道防线：
    - **不让它产生**：`run_command`（Bash 和 RunTests 都经过它）运行前把引号外、作为重定向目标的 `nul`（`>nul`、`2>nul`、`&>nul`、`>> NUL`，不分大小写）改成 `/dev/null`；Bash 的结果在 `[exit code N]` 之前加一行说明，不然模型下次还这么写。`nul.txt`、`./nul`、引号里的 `nul` 不动。Linux 上也照改：那里 `nul` 同样只是个文件名。
    - **有了也不卡提交**：垃圾规则排除任何一层名叫 `nul` 的文件（不分大小写），`add -A` 碰不到它。只排除这个名字本身：`2>nul` 产生不了 `nul.txt` 之类，别的名字不该被悄悄拦下。
    - **删得掉**：`remove_tree` 在 Windows 上走 `\\?\` 字面路径，那里 `nul` 就是个普通文件。

44. **文件夹没了的 chat 只列出来让人删，从不自动删。** 项目文件夹删掉以后，它的 chat 打不开了，记录却还留在数据库里。可是"文件夹不在"和"文件夹暂时够不着"看起来一模一样：服务器上的网络家目录暂时读不到、移动硬盘拔了、网络盘断了、文件夹改了名，都是这样。自动删就会把还有用的记录悄悄删掉。所以项目菜单里只给一个"清理"入口，确认框按文件夹列出、默认全选、写明"只是暂时够不着的别删"，由人勾选后再删。删的只是记录：任务和事件、这个文件夹在"最近的项目"里的那一行和它的模型设置（这个文件夹的 chat 全删光了才动这两样），以及只被这些 chat 用过的附件（还有别的任务在用的留着；还没发出去的附件不属于任何被删的任务，不会被牵连）。服务端删之前再查一次文件夹，还在的、正在跑的一律不删，所以请求里夹带什么都删不到活的 chat。git 分支不动：文件夹是仓库里被删的子目录时，仓库还在，chat 分支和上面的提交留在那里，仓库状态里照样列着它。

## 5. 事件（`on_event` 收到的 dict）

| type | 字段 | 写入数据库 |
|---|---|---|
| `task_record` | chat_id, task_id | ✅ |
| `task_start` | task, model, provider, work_dir, thinking, vision, attachments（元数据加 path） | ✅ |
| `step_start` | step | ✅ |
| `reasoning_delta` / `content_delta` | text | ❌ |
| `assistant` | text, reasoning（这一步完整的回复，供回放） | ✅ |
| `handoff_start` | （之后的 content_delta 都是交接笔记，不是某一步的回复） | ✅ |
| `tool_call` | id, name, args | ✅ |
| `tool_output` | id, text（Bash / RunTests 流式输出） | ❌ |
| `tool_result` | id, name, output, is_error, elapsed, images（这次结果里带了几张图） | ✅ |
| `plan` | id（那次 Plan 调用的 id）, steps（step, status）, notes：整理过的计划，界面和终端照它画清单 | ✅ |
| `usage` | step, model, prompt/completion/reasoning/cached_tokens, context_tokens, context_limit, ttft, elapsed, tokens_per_second, cost, total_cost | ✅ |
| `handoff` | source（model 或 digest）, notes | ✅ |
| `continuation` | session | ✅ |
| `steer` | text, attachments | ✅ |
| `review` | model, clean, findings（审查者读完 diff 的结论） | ✅ |
| `subtask_start` / `subtask_finish` | task_id, headline, read_only, budget / success, steps, total_tokens, summary | ✅ |
| `usage` 里的监控字段 | handoff_at（这一段会话在多少 token 处交接）、total_tokens（本任务累计）、total_cost | ✅ |
| `model_switch` | model, previous, provider, by（agent / user / fallback）, reason, local, vision, thinking, context_limit | ✅ |
| `worktree` | branch, base, path, created, restored, carries_user_changes | ✅ |
| `task_committed` | branch, commit, subject | ✅ |
| `notice` / `retry` | text / attempt, error, delay | ✅ |
| `finish` | success, summary, reason, steps, sessions, context_tokens, total_tokens, elapsed, cost | ✅ |

`reason` 的取值：`finished`、`implicit_finish`、`max_steps`、`sessions_exhausted`、`stopped`、`error`；在数据库里还有 `running` 和 `interrupted`。

**通过 WebSocket（`/ws`）传给界面的消息：** 服务端会给上表的每个事件加上 `chat_id` 和 `task_id` 再转发，另外还有以下几种：

| 方向 | type | 字段 |
|---|---|---|
| 服务端 → 界面 | `hello` | state（busy、chat_id、task_id、task）、backlog（当前任务到目前为止的事件） |
| 服务端 → 界面 | `accepted` / `task_started` / `task_finished` | chat_id；task_started 还带 task、request 和 attachments；task_finished 还带 success 和 reason |
| 服务端 → 界面 | `approval_request` / `approval_resolved` | id, command, reason / id, allowed |
| 服务端 → 界面 | `question_request` / `question_resolved` | id, question, options / id, answer（AskUser） |
| 服务端 → 界面 | `repo` | work_dir, repo, branch, dirty_files, pending（任务结束、合并、丢弃后推送） |
| 服务端 → 界面 | `error` | message |
| 界面 → 服务端 | `run` | work_dir, task, chat_id?, base?, isolate, model?, thinking?, max_steps, max_sessions, plain_commits, attachments（id 列表）, allow_cloud, review |
| 界面 → 服务端 | `steer` / `stop` / `approval` | text, attachments? / – / id, allow |
| 界面 → 服务端 | `set_model` | model（把正在跑的任务换到这个模型） |
| 界面 → 服务端 | `answer` | id, text（回答 AskUser 的问题） |
| 服务端 → 界面 | `remote_status` | name, state（disconnected / connecting / connected / failed）, detail, ui_url, skills（本机 skill 在那边的副本情况，如 "2 skills copied"）, refused（因为别的用户在监听转发端口而断开，不变量 41）（不进 backlog） |
| 服务端 → 界面 | `ssh_prompt` / `ssh_prompt_resolved` | id, remote, prompt, kind（secret / confirm）/ id, remote（**不含答案**，不进 backlog；刷新后从 `GET /api/remotes` 取） |

**注册表和附件的 REST 接口**（都要 token）：

| 方法 | 路径 | 用途 |
|---|---|---|
| POST / GET | `/api/attachments?name=…` · `/api/attachments/{id}` | 上传（请求体就是文件内容）、取回原件 |
| GET | `/api/files?work_dir=…` · `/api/scratch` | @ 提及用的文件清单；没有项目时用的临时文件夹 |
| GET | `/api/registry` | provider、模型、角色、预设（密钥只给 `has_key`） |
| PUT / DELETE | `/api/providers/{name}` | 新增或修改（`api_key` 会写进 secrets.env）、删除（`?forget_key=true` 连密钥一起删） |
| GET | `/api/providers/{name}/discover` | 探测这个 provider 有哪些模型（Gemini 用原生接口，不耗生成额度） |
| PUT / DELETE | `/api/models/{name}` | 新增或修改模型、删除（被角色占用时返回 409） |
| PUT | `/api/roles` | 设置角色（值为 null 表示取消，main 不能取消） |
| POST | `/api/models/{name}/test` | 快速测试：1 次工具调用请求，能看图的再加 1 次看图请求 |
| GET | `/api/skills?work_dir=…` | 这个文件夹能用的 skill（source 为 project / user / synced） |
| PUT | `/api/skills/synced?origin=<电脑名>` | 请求体是 zip：另一台电脑的 app 把它的用户 skill 推过来（用本机这台服务器的 token） |
| GET / PUT | `/api/web` | 联网设置（`api_key` 写进 secrets.env，只返回 `has_key`） |
| POST | `/api/web/search` | 用当前设置搜一次，给设置页的"Try a search" |
| POST | `/api/remotes/{name}/run` | 跑这台机器"有名字的命令"（请求体只给名字），返回它打印了什么和退出码 |
| GET / PUT | `/api/settings` | 界面跨启动记住的设置（`settings` 表）；PUT 只带要改的键，值为 null 表示删掉 |
| GET / POST / DELETE | `/api/recent` | 最近的文件夹（最新在前，已不存在的和 scratch 不列）/ 记下打开了一个文件夹 / 从列表里去掉（`?path=`，chat 不受影响） |
| DELETE | `/api/chats/{id}` | 删掉一个 chat 和它的记录，连同它放进文件夹、别的 chat 没用到的附件副本（不变量 18）；分支上还有提交或未提交改动时返回 409，加 `?discard_branch=true` 才连分支一起丢（空分支直接删） |
| GET / POST | `/api/orphaned-chats` · `/api/orphaned-chats/delete` | 文件夹已经不在的 chat / 删掉请求体 `ids` 里选中的那些（服务端再查一次文件夹，还在的、正在跑的一律不删），返回删了几个 chat、任务、附件 |
| GET | `/api/tree?work_dir=…&path=…` · `/api/tree/changes` | 目录树的一层 / 整个根的改动清单；用 `chat_id=` 代替 `work_dir` 就是那个 chat 的 worktree |
| GET | `/api/file?work_dir=…&path=…` · `/api/file/raw` | 一个文件的内容和 diff / 图片原件（带 `Content-Security-Policy: sandbox`，SVG 里的脚本不会运行） |

## 6. 数据文件
这些文件都在**检出目录的 `data/` 下**（`LOCALAGENT_HOME` 可覆盖；装成 wheel 时是 `~/.localagent`）。`data/` 在 .gitignore 里，所以 secrets.env 不会被提交。

- **`data/models.yaml`：** 包含 providers、models、roles 三部分（角色的 model 可以是 `auto`），格式见 `models/registry.py` 和 design.md 第 3 节。**不存密钥**，只写 `api_key_env`，即密钥所在的环境变量名。
- **`data/secrets.env`：** `名字=密钥` 每行一条。命令行用 `la secret set/list/remove`，界面里填 provider 的 API key 也写到这里。读取顺序是先环境变量、再这个文件；读出来的值不会放进 `os.environ`。
- **`data/web.yaml`：** `enabled`、`search`（duckduckgo / searxng / brave / tavily / off）、`url`、`api_key_env`（默认 BRAVE_API_KEY / TAVILY_API_KEY）。没有这个文件时等于 duckduckgo、开启。
- **`data/synced-skills/<电脑名>/`：** 那台电脑的用户 skill 的副本，由它的 app 在连接期间推过来，每次整份替换；以点开头的目录是正在解压的临时目录，加载时忽略。
- **`data/scratch/`：** 没有项目的 chat 在这里干活（真实目录，工具照常可用；永远不是 git 仓库，也永远不做分支隔离 —— 即使你的用户目录本身是个仓库）。
- **`data/attachments/<id>/`：** 附件原件（`content`）、元数据（`meta.json`）、文本（`text.txt`，PDF 抽出的或文本文件本身）。`<id>` 是"文件名 + 内容"的哈希，所以同一个文件不会存两份。
- **git 仓库里：**
  - 分支 `la/<chat>`
  - 目录 `<repo>/.la-worktrees/<chat>`，在 `.git/info/exclude` 里写了 `/.la-worktrees/`，所以 git 不会把它当成未跟踪文件
  - 配置项 `branch.la/<chat>.la-*`
  - 锁文件 `.git/localagent.lock`
- **`data/localagent.db`：**
  - `chats(id, created, work_dir, title, branch, base)`（branch、base 两列会自动迁移到旧库）
  - `folders(path, opened, forgotten)`：界面打开和移除文件夹的时间。最近的项目 = 打开过的文件夹和有 chat 的文件夹，按最后一次使用排序，去掉最后一次使用早于移除时间的
  - `settings(key, value)`：界面要跨启动记住的东西（`thinking`、`allow_cloud`、`review`、`sidebar_tab`，以及每个项目一条的 `model:<工作目录>`），值是 JSON
  - `tasks(id, chat_id, created, finished, task, result, success, reason, model, work_dir, steps, sessions, total_tokens, elapsed, attachments, cost, parent_id, plan)`（后四列会自动迁移到旧库；`parent_id` 不为空的是子 agent 的运行记录，不计入 chat 的任务数；`plan` 是任务最后记下的计划清单）
  - `events(task_id, seq, ts, type, data JSON)`，`(task_id, seq)` 唯一
  - 用 WAL 模式，一个连接加一把锁。

## 7. 与 KISS Sorcar 的对比

### 7.1 相同或照搬的
- L1：用原生 function calling 加 `finish` 工具；对话只追加，用量信息附在最新消息后面；模型只回文字时先提醒，连续两次就当作隐式结束；工具报错时把期望的签名一起告诉模型；可重试的错误最多重试 3 次。
- L2：续跑靠"按时间顺序、带代码片段"的交接笔记，**不在原对话里压缩**；续跑说明照搬了"不要重做"和"原地打转就换思路"；笔记总长有上限，优先保留最新的。
- L3：工具名沿用 Read / Edit / Write / Bash，这是模型训练时见过的名字；临时文件放 ./tmp。
- L4：历史只保留最前 2 条和最新 8 条，放在第一条 user 消息里；每个任务记录模型、步数和 token。
- 运行中插话：每步开始前把消息加进对话；有未处理消息时拒绝 finish。
- 系统提示词：借鉴了"结束前逐条核对需求"、"同一种修法失败 3 次就换思路"，以及长命令用 `nohup` 并完整重定向输出后放到后台。

### 7.2 为本地模型做的优化（KISS 的做法和我们的做法）
| # | 问题 | KISS | LocalAgent | 依据 |
|---|---|---|---|---|
| 1 | 跨任务的前缀缓存 | 系统提示词里有开始时间、task id 等每次都变的值 | 系统提示词在同一工作目录下字节一致，变化的内容放到第一条 user 消息 | 缓存命中与否：0.8 秒对 48 秒 |
| 2 | 交接笔记怎么来 | 靠提示词让模型在约 80% 时主动 `finish(is_continue)`；到 90% 时由 Summarizer agent 读轨迹文件（冷启动加多步） | 由程序在阈值处触发，并且**在同一段已缓存的对话里**要笔记，工具不变、关思考；只在拿不到时才用机械版摘要兜底 | 写笔记只需付生成的时间；小模型不擅长自己盯着上下文用量 |
| 3 | 笔记会越积越多 | 所有旧摘要都带上（最多 6 万字符） | 交接请求要求新笔记"覆盖旧笔记并简述之前的工作"，只带最新一份模型笔记，外加它之后的机械摘要 | 5.5K 窗口实测：旧做法 6 个会话都用完，一直在重复验证 |
| 4 | 新会话起点就接近阈值 | 不处理 | `handoff_threshold`：每个会话至少能增长窗口的 25%，最多到 90% | 同上 |
| 5 | 新会话重复验证 | 不处理 | 续跑说明加一条"相信笔记；笔记说已完成并验证过，就直接 finish" | 同上 |
| 6 | 先读后改 | 只写在提示词里 | 代码里用哈希检查，文件被外部改过会拒绝；每个会话重置 | 小模型不会严格遵守规则 |
| 7 | 提示词体量 | SYSTEM.md 约 5K token，加 20 多个工具约 1.2 万 token | 约 1.8K token，7 个工具 | 本地冷启动 prefill 约 245 tok/s |
| 8 | 工具输出 | Bash 最多 5 万字符 | 8K 字符，保留头尾；RunTests 第一行就是结论 | 每 1K token 的输出要多 4 秒 prefill；避免 `pytest \| tail` 把退出码吃掉 |
| 9 | 历史结果长度 | 不截断 | 每条截到 2000 字符 | 本地窗口比云端小 |
| 10 | 模型路由 | 按名字前缀路由，自定义 endpoint 全局只有一个，而且会一直带到后续模型上 | 以 provider 为中心的注册表，按角色分配模型，可以按请求开关思考 | 要支持本地和云端混用 |
| 11 | 并发 | 不限制 | 每个 provider 一个信号量 | LM Studio 只有 2 个并发槽，槽被占时还可能把前缀缓存挤掉 |
| 12 | Windows | `which bash` 会找到 WSL；没有 fcntl，跨进程锁失效 | 从 git 的安装目录找 Git Bash，用 `taskkill /T`，统一 UTF-8，能读 GBK 文件 | 在本机实测 |
| 13 | 数据库写入 | 所有流式 delta 都写，所以要后台批量写线程 | 只写对话级别的事件，同步写即可 | 更简单 |
| 14 | 隔离的单位 | 每个任务一个分支；下一个任务开始时，自动把上一个成功的任务合并进用户分支 | 每个 chat 一个分支，每个任务一个提交；只有用户明确执行 merge 才会合并 | 后续任务自然能看到之前的改动；用户分支从不被自动改动 |
| 15 | 合并的位置 | 必要时在用户的检出里切换到原分支 | base 分支在哪个 worktree 被检出，就在那里合并；没被检出就用临时 worktree，从不切换用户的分支 | 更安全 |
| 16 | 大量未跟踪文件 | `git add -A` 加 `--include-untracked` 的 stash | 复制和提交都排除垃圾文件；stash 只处理已跟踪文件 | 示例项目里就有一个没被忽略的 `.venv` |
| 17 | worktree 里的 venv | 不处理（KISS 靠 uv） | 依次在 worktree、用户目录、仓库根目录找 `.venv` | worktree 里本来没有 `.venv` |
| 18 | 越出 worktree | Bash 里拦截提及父仓库路径的命令 | 同样的思路，同时认 `C:\`、`C:/`、`/c/` 三种写法；写文件的工具只能写 worktree 内部 | |
| 19 | 图片和文档 | Read 把二进制文件转成附件塞进对话；没有"用户在对话里发文件"这一步 | 用户发的文件落到工作区的 `.la-attachments/`：图片作为 `image_url` 块发给能看图的模型，PDF 先抽成文本（Read 和 Grep 都能用），小文档直接放进第一条消息 | 本地窗口装不下整篇论文；纯文本模型也要能用上文档 |
| 20 | 模型选择 | 每个 agent 启动时定好模型 | 任务中途可以换模型（`set_model`），对话原样带过去，只在发请求时按目标模型适配 | 本地模型不能看图，可以临时借云端模型的眼睛，再换回来继续改代码 |
| 21 | 密钥 | 环境变量 | 环境变量或 `data/secrets.env`；不进 `os.environ`，所以 agent 跑的命令看不到 | agent 会执行任意命令，密钥不该顺手暴露给它 |
| 22 | 计划 | 提示词里的规则（论文 §6.1：3 个以上文件先列出每个文件改什么、顺序、怎么验证），写在回复里；跨会话靠 `./tmp/PROGRESS.md` | `Plan` 工具：参数留在对话里，交接和"再试一次"都带过去，界面画成清单 | 27B 模型不照提示词写提纲，而是在第一步的思考里设计整个项目，之后每步重想（不变量 42） |

### 7.3 KISS 里值得以后加上的
- **M3 已采用：**
  - 用 baseline commit 加 `cherry-pick --no-commit` 合并，`-X theirs` 的使用条件与 KISS 相同
  - 状态存在 git config 里
  - 提交消息附上任务和结果
  - 跨进程锁（Windows 上用 msvcrt 实现）
- **M3 没采用：**
  - 回收孤儿 worktree：我们从不自动合并，所以用不上，`la status` 能列出所有待处理分支。
  - 预建 worktree 池：本地的瓶颈在模型，不在 `git worktree add`。
  - 抢救被 gitignore 的产物：`la keep` 会保留分支，worktree 目录只在 merge 或 discard 时才删除；以后如果有需要再加。
- **M4 已完成：** 本地服务加 WebSocket 协议（命令和事件的分法参考了 KISS），支持运行中插话、停止、审批、差异视图、合并/保留/丢弃、中途连上的客户端追上当前进度。
- **M5 已完成：** 云端 provider（预设加密钥管理）；跨 provider 的 `set_model`（对话原样带过去，换过去之后是冷启动，失败会退回原模型）；界面里添加、探测、测试模型和设置角色；图片和文档附件（见第 7.5 节）。
- **M6 已完成：** AskUser、按步计价、审查者角色（一个写一个审）、@ 文件提及、PDF 页面渲染成图片、没有项目的 scratch chat，以及九个 bug 的修复（见 7.7）。
- **以后：**
  - "建议的下一步"（`suggested_next_task`）
  - 常用任务，常用指令片段加输入补全
  - 从历史里的某个任务接着做（沿 parent 链取历史）
  - 真正的并行（只在"本地 + 云端"时才有意义：两边不共享 KV）
  - 云端硬预算上限（现在只统计，不拦截）
- **暂不做：** 浏览器自动化、语音、第三方消息 agent、Docker 模式、MCP。（Skills 在 M8 做了，联网搜索和读网页在 M10 做了。）

### 7.4 界面上显示分支（M4 已实现）
- 顶栏显示你检出的当前分支、未提交数量、待处理的 chat 分支数量。
- chat 标题栏显示这个 chat 的分支，比如 `la/<chat> ← main · N 个提交 · M 个文件`。
- 侧栏每个 chat 旁边标出它有几个提交，或显示"已合并/已关闭"。
- 数据来源如下：
- **状态来自 `worktree.repo_status(work_dir)`：**
  - 当前分支
  - 未提交改动的数量
  - 每个待处理的 chat 分支，包括它的 base、提交数、改动文件数，以及 worktree 目录还在不在
- **实时变化来自事件：**
  - `worktree`：这个任务在哪个分支上、从哪个分支来、是新建的、续用的还是恢复的
  - `task_committed`：任务结束后产生的提交
- **界面操作直接对应现有函数：** 选 base 分支就是 `git.local_branches` 加 `ChatSession(base=...)`；合并、保留、丢弃分别是 `merge()`、`keep()`、`discard()`，差异视图用 `diff()` 和 `commits()`。
- 因为状态都存在 git 里，所以即使 CLI、界面和手机同时打开，或者进程崩溃后重启，看到的都是一致的。

### 7.5 附件与换模型（M5 已实现）
- **用户发来的东西怎么走：** 界面上传 → `data/attachments/<id>/`（按内容寻址）→ 任务开始时 `place()` 复制进工作区的 `.la-attachments/` → 第一条 user 消息里列出清单，图片作为 `image_url` 块跟在后面，小文档正文直接内联。
- **各种类型：**
  - 图片：发给能看图的模型（`ModelSpec.vision`）；别的模型看到的是一句"这里有张图，当前模型看不了"，路径仍然给它，所以还能复制、移动、交给别的模型看。
  - PDF：抽成文本存成 `<名字>.pdf.txt` 放在同一个目录，Read 分页读、Grep 能搜；小于 8000 字的直接内联。
  - 文本文件：同上。
  - 其他二进制：只放文件，不解析。
- **界面：** 输入框支持粘贴、拖拽、选文件；图片可以点开画框、箭头、涂画（`Annotator`），标注会烧进图片本身；超过 1600px 的图片先缩小，避免本地模型 prefill 爆炸。当前模型看不了图时输入框会提示，并给出可以切换的模型。
- **换模型：** `set_model` 工具只列出勾了"允许 agent 切换"的模型，云端的还要打开"cloud"开关；工具描述里带上每个模型的位置、上下文长度、能不能看图，以及备注（备注就是给模型看的"什么时候用我"）。用户也可以在运行时用顶栏的下拉框直接把任务换到任意模型。
- **实测（2026-09-11）：** 本地 qwen3.8-27b 拿到一张标注过的设计稿后，先 Read 图片（被告知看不了），再调用 `set_model` 换到 gemini-flash-latest 去看图——事件和消息都正确；该次云端请求返回 401（密钥当时已失效），任务因此退回本地模型继续（这一步就是第 4 节不变量 20 的由来）。另一次纯本地的运行：5 步、69 秒改好 CSS，附件留在 `.la-attachments/` 里没有进提交。

### 7.6 读完论文之后的对照（arXiv 2604.23822，2026-06）

**骨架是一样的。** 五层、continuation（不压缩、写结构化笔记再开新会话）、worktree 分支隔离、任务中途 `set_model`、运行中插话、chat 历史取前 2 条加最近 8 条 —— 这些我们都照着做了，论文里的理由和我们实测到的现象一致。论文自己也强调"没有用轨迹压缩、没有异步多 agent 编排也够用"。

**论文有、我们没有的：**

| 能力 | 论文里的用途 | 我们的判断 |
|---|---|---|
| `run_parallel` 并行子 agent | 并行跑测试分片、并行调研 | **不做**：单卡上的 LM Studio 只有 2 个槽且 Unified KV 共享 122,880，并行会挤掉主 agent 的前缀缓存，得不偿失。云端模型多了以后可以再考虑 |
| 浏览器自动化（Playwright） | 验证网页、查文档 | 暂不做；我们改用"用户截图 + 标注"来覆盖"看看页面对不对"这个主要场景 |
| MCP / Skills / 23 个消息 agent / Docker | 生态和沙箱 | 暂不做 |
| 504 个模型的目录 | 开箱即用 | 我们用 provider 预设加自动探测，本地模型的真实参数（加载上下文、视觉）只有服务器知道 |
| 硬预算上限（超了就报错） | 防止云端烧钱 | 我们只做了成本统计；本地模型免费，云端目前是"看得见"而不是"拦得住"。要加上限很容易 |
| AI Discovery / GEPA 模板 | 拿 agent 当研究驱动器 | 与本项目目标无关 |

**我们有、论文没有的（几乎都是被本地模型和 Windows 逼出来的）：**
- **前缀缓存是硬不变量**：系统提示词在同一工作目录下字节一致，每任务变化的内容进第一条 user 消息。论文的系统提示词里有版本号、PID、work_dir、step 阈值，每次都变；在云端无所谓，在本地是 0.8 秒对 48 秒的差别。
- **`auto` 角色**：跟着服务器当前加载的模型走，并采用它实际加载的上下文长度。单卡机器上这条避免了"写死模型名 → 下一个任务把加载的模型顶掉"。
- **按模型适配消息**：没有视觉的模型把图片换成一句说明，Gemini 的 thought signature 原样带回 —— 换模型不会把对话弄坏。
- **换过去的模型失败就退回**原模型继续（实测密钥失效时救回了一个任务）。
- **密钥隔离**：密钥不进 `models.yaml`，不进 `os.environ`，也不进 agent 执行命令时的环境。
- **附件**：图片和 PDF 进任务，PDF 抽文本给 Read/Grep 用。论文只有"Read 把二进制当附件带上"。
- **Windows**：Git Bash、`taskkill /T`、GBK、只读 git 对象、子进程不弹控制台窗口。

**系统提示词的取舍。** 论文的 `SYSTEM.md` 约 5K token 加二十多个工具（约 1.2 万 token 的前缀）；我们约 2K token 加 8 个工具。差别不只是长度：
- 论文用大量提示词规则去约束模型（"禁止用 cat 读文件"、"必须先 Read 再 Edit"、"第一件事必须读 SORCAR.md"），并给出了实测的失败率：91 个真实任务里 **92% 没读 SORCAR.md**、**70% 跳过 lint/typecheck**。
- 我们能用代码保证的就不写进提示词：先读后改是**哈希校验强制**的；项目说明文件（CLAUDE.md/AGENTS.md）**直接拼进系统提示词**，不需要模型自己去读；测试结论由 RunTests 直接给出第一行，不依赖模型看懂 pytest 输出。
- 论文里"靠提示词才能做到"的几条，这次我们补了进去（跨 3 个文件以上先列计划、对齐类任务必须先读目标、结束前重读改过的文件并清理 ./tmp、跑一次 linter）。
- 论文**删掉过**一个我们本来就没做的东西：`USER_PREFS.md` 自学习文件。他们发现它积累的"项目事实"会过期，反而让 agent 重新引入已经修好的 bug。这验证了我们把项目知识放在用户手写的 CLAUDE.md 里的选择。

**哪个更好？分场景，而且不该拿分数比。** 论文的 62.2% 是 Claude Opus 4.6 在 Terminal Bench 2.0 上的成绩，主要反映模型本身；我们没有可比的评测，也不该声称有。
- **前沿云端模型 + 大型任务**：KISS Sorcar 更强 —— 并行子 agent、浏览器、生态、更细的提示词纪律，都是在"模型很聪明、token 不心疼"的前提下才划算的设计。
- **单卡本地模型**：我们更合适 —— 缓存不变量、短提示词、`auto` 跟随加载的模型、按模型适配消息、图片/PDF、以及一整套 Windows 细节，都是论文没有面对的问题。
- **两者的共同结论**：结构化交接优于压缩上下文；worktree 隔离比让 agent 直接改用户工作区安全；`set_model` 让"便宜模型干活、强模型把关"成为一句话的事。

### 7.7 审查者、@ 提及、PDF 图片、没有项目的 chat（M6）
- **审查者（`--review` / 顶栏的 review 开关）：** 任务成功后、提交之前，用 `reviewer` 角色的模型读这次任务**还没提交**的 diff（`working_diff` 用 `git add -N` 让新文件也出现在 diff 里）。它只有只读工具，用 `finish(success=true)` 表示"没问题"、`finish(success=false)` 加编号清单表示"有问题"。有问题就把清单交回主模型修，修完**和原改动一起提交一个 commit**，摘要里保留三段：原始结果、审查发现、修复说明。它必须**自己用 RunTests 跑一遍测试**（检测到的命令不需要审批，无人值守也能跑；自定义命令走 `guard_command`，审批器由 `ChatSession` 传进来），并且判断这些测试**证明了什么**：把被测对象换成 stub、只断言调用发生过、或者改动还原后照样能过的测试都算问题。这一条对应 KISS 论文里「要真实沙盒测试、不要 mock」的要求，区别是我们把它放进审查者的提示词，而不是指望主模型自觉。角色没单独配就用同一个模型的新上下文（仍然有效，只是不如跨厂商）。实测：主任务 8 步 65 秒，审查 4 步 26 秒。
- **@ 文件提及：** 输入框打 `@` 弹出这个文件夹的文件（`/api/files`，和 agent 的 Grep 看到的是同一份清单，遵守 .gitignore），按文件名匹配度排序，回车或点击补全成路径。省掉"你指的是哪个文件"这一轮。
- **PDF 里的图：** `Read(file, page_number=N)` 用 pypdfium2 把那一页渲染成 PNG 给能看图的模型（PNG 直接从渲染缓冲编码，不需要 Pillow）。抽出来的文字里没有图表，这是唯一能看到它们的办法；附件说明里只对能看图的模型提这件事。
- **没有项目的 chat：** `data/scratch` 是一个真实文件夹，所有工具照常可用，但永远不是 git 仓库、永远不做分支隔离（就算你的用户目录本身是 git 仓库也不会）。界面首页有入口，命令行是 `la run --scratch`。系统提示词里那一行会告诉模型"这是临时文件夹，不是项目，答案直接写进 finish"。

### 7.8 子 agent：为什么是顺序执行的（M7）
论文的 `run_parallel` 是并行跑多个子 agent。在一张 2080 Ti 上实测过：单请求 32 tok/s，两个并发各 13–14 tok/s，**总吞吐反而略降**，所以并行本身不带来收益。真正的收益是**上下文经济**：一次翻代码库可能在主对话里留下 8–15K token 的工具输出，交给子 agent 之后回来的只有几百 token 的报告，主 agent 的上下文不涨，后面每一步都更快，交接也更晚。

因此我们的实现是：
- **顺序执行**（调用方等着），同一时刻只有一个请求在飞；
- **预算 = 调用方窗口 − 已用 − 保留额度**，上限 32K、下限 8K，不够就拒绝并让调用方自己做。这条是关键：两段对话同时待在共享 KV 里，如果挤掉调用方 70K 的前缀，重新 prefill 要 ~5 分钟，比子任务省下的多得多；
- **子 agent 不能再开子 agent**，默认只读；
- 它的完整过程存成一条 `parent_id` 指向调用方的子任务，`la show` 会列出来。追溯的顺序是：先看 `git diff`，不够再翻这条记录。

实测（2026-09-12，同一个任务跑两次，唯一区别是有没有 Task 工具）：主 agent 上下文峰值 8,833 对 57,925（6.6 倍），全过程 prompt token 78K 对 301K，但**墙钟时间 301 秒对 223 秒——对照组更快**，因为主对话的前缀一直命中缓存而每个子 agent 都要冷启动。省下的上下文体现在速度曲线上：对照组从 49 tok/s 掉到 30 tok/s、首 token 从 2 秒涨到 16 秒，并且停在离交接阈值一步之遥的 58K。数据和结论见 [design.md §1.5](design.md)。

### 7.9 Skill：按需打开的说明书（M8）
论文里没有这一层，但对本地模型价值很大：一个 27B 记不住你项目的约定，而把约定写进 `CLAUDE.md` 又会让**每个**任务都背上这些 token。Skill 把两者分开——

- 目录（`skills.catalogue`）跟在 `<project_instructions>` 后面进系统提示词：每个 skill 一行，约 15 token，20 个约 300 token（按实测 245 tok/s 的冷启动 prefill 算多约 1.2 秒，之后一直命中缓存）。
- 正文由模型自己用 `Skill(name)` 取回，作为工具结果**追加**，不碰前缀。这就是 progressive disclosure，正好落在我们"对话只追加"的不变量上。
- 来源三处：`<work dir>/.localagent/skills/`（跟着仓库）、`data/skills/`（所有项目）、`data/synced-skills/<电脑名>/`（从登录过来的电脑同步的，见 7.11），同名时依次优先。格式用 Claude Code 的 `SKILL.md`，没有 YAML 头也能读（文件夹名当名字、第一行当描述）。
- skill 文件夹里的脚本和参考资料就是普通文件：Read 可以读工作区之外的文件，所以全局 skill 的脚本也能直接读和跑；写入仍然只限工作区之内。

### 7.10 从界面登录其他机器（M9）
典型场景：GPU 服务器要经过跳板机，而且登录要输密码（没配 key，或者 key 暂时不被接受），以前只能先开终端登一次。现在这一步在 app 里完成，做法和 VS Code Remote-SSH 一样——

- **不自己实现 SSH。** 调系统自带的 `ssh`，读用户的 `~/.ssh/config`，ProxyJump、key、主机指纹全部照旧。Python 的 SSH 库对 ProxyJump、`Match` 块、多跳密码提问的支持都不可靠。
- **`SSH_ASKPASS_REQUIRE=force`** 让 ssh 在没有终端时也把提问交给 `la-askpass`（在用户这台 Windows 的 OpenSSH 9.9 上实测过，ProxyJump 派生的那个 ssh 也会用它）。la-askpass 是 GUI 入口，由 `pythonw` 运行，不弹窗口，答案通过管道交还给 ssh。
- **一台机器一条 ssh。** 这条连接同时带上所有端口转发（模型服务、界面）和 UI 命令：`<端口检查> && <ui> && echo LA_REMOTE_READY && exec <每 2 秒一次的端口检查>`。一条连接只登录一次：有跳板机时就是跳板一次、目标一次。重连时重新执行 UI 命令，所以那边的界面重启换了 token 也能拿到新地址。
- **"连上"的判断**：那边报告了端口检查的结果（不变量 41），本机转发端口都能连通，并且（有 UI 命令时）看到了 READY 和界面地址。开始前先检查本机端口有没有被占用——用户以前手开的隧道还在时，不检查的话会误报"已连接"，请求其实全发到旧隧道上。
- **重连**：连上过又断了，按 2/5/15/30/60 秒退避重连（一次密码登录之后 key 又能用，通常不再提问）；**从没连上**（密码错、取消、连不上）就停下，等用户再点，不会反复弹密码框。
- **机器上有名字的命令**（`commands:`）：`start` / `stop` / `status` 这类平时要 ssh 进去敲的东西，写进 `remotes.yaml` 后在 Machines 里变成按钮，各起一条短 ssh 跑完就退，输出显示在对话框里（`la remote run <机器> <命令>` 是同一条路）。**接口只收名字，不收命令行**：能触发的永远只有用户自己写在 `remotes.yaml` 里的那几条。它的 ssh 要密码时照样在界面里问（一次性口令的机制和隧道共用），取消同样是先杀 ssh 再放行 la-askpass。
- **切到那台机器**：连上后把窗口导航到那边的界面地址，并在片段里加 `remote=<名字>`。那边的页面据此显示"在 gpu1 上"和返回按钮（`history.back()`）。**不把本机的 token 传给那边的页面**：那边的服务器如果被攻破，拿着本机 token 就能在你的笔记本上执行命令。

实测（2026-09-13，笔记本 → 跳板机 → GPU 服务器，当时服务器只接受密码登录）：连接后 2.1 秒，界面收到 ssh 原样的 `<用户>@<服务器>'s password:`；点取消后 ssh 立即结束、没有发出任何密码，进程全部退出；所有 ssh 和 la-askpass 进程都没有可见窗口。

### 7.11 联网搜索与 skill 同步（M10）
**联网。** 本地 27B 的知识停在训练截止日，冷门的东西也记不全；研究和写代码最常见的错误就是"凭记忆写了一个不存在的参数"。所以给它 WebSearch 和 WebFetch，系统提示词里加一句"拿不准就查，并写出依据的网址"。取舍：
- **搜索服务是设置，不写死。** 默认 DuckDuckGo 简版网页：零配置，但可能把服务器 IP 当成机器人拦下（2026-09-15 实测：`html.duckduckgo.com` 返回 202 验证页，`lite.duckduckgo.com` 正常出结果），被拦时报错里直接说该换哪家。要稳定就用自己的 SearXNG（`scripts/gpu-server/setup-searxng.sh`；GPU 服务器上实测 DuckDuckGo 简版页同样返回 202，而 SearXNG 经 Google CSE 和 Brave 一次约 0.7 秒出 30 条结果），或者 Brave / Tavily 的 key。
- **WebFetch 给文本不给 HTML。** 本地 prefill 按 token 算钱，一个 GitHub 页面的 HTML 是正文的几十倍。用标准库 `html.parser` 去掉脚本、导航、页脚、表单，页面标了 `<article>` 就只取它（GitHub 的 `<main>` 里还包着工具栏），否则 `<main>`，都没有就取全文；标题、列表、表格、代码块保留结构，页内跳转链接丢掉，其余链接改成绝对地址。一次 1 万字，`start` 分段读，同一工具集内缓存 16 页。PDF 复用附件的抽文字。实测：llama.cpp 的 speculative.md 页面 24K 字符，第一段直接从 "# Speculative Decoding" 开始；arXiv 2211.17192 的 PDF 13 页 47K 字符。
- **只读 agent 也有。** 读网页不改磁盘，子 agent 做调研正好把大段网页挡在主 agent 的上下文之外。

**skill 同步。** 用户在笔记本上写 skill，任务却常常跑在 GPU 服务器上，那边读的是自己的 `data/`。app 连接机器时已经有了隧道和那边界面的地址（带那边的 token），所以同步不需要再登录一次：本机每 5 秒算一次 skill 目录的指纹（文件名、大小、修改时间），变了就打 zip 经隧道 `PUT /api/skills/synced`。方向只能是本机到服务器（不变量 30）。上限 20 MB、2000 个文件，跳过 `__pycache__`、`.git`、`.venv`、`node_modules`；Windows 打的 zip 没有可执行位，Linux 上解压时给以 `#!` 开头的文件补上。服务器上的 LocalAgent 是旧版本时，本机有 skill 才提示"那边太旧"，没有 skill 就不打扰。

## 8. 扩展指南
- **加工具：** 在 `tools/` 写一个返回 `Tool` 的工厂函数，出错时抛 `ToolError`，然后在 `tools/__init__.make_tools` 里注册。别忘了工具定义本身也占 prefill，描述要短。
- **加一个云服务：** 多数服务（包括 Gemini 和 Anthropic）都有 OpenAI 兼容端点，所以只要在 `models/presets.py` 的 `PRESETS` 里加一条（地址、密钥变量名、并发数）；如果它的模型列表能给出上下文长度，再在 `discovery.py` 里加一个分支。
- **加 provider 类型（协议真的不一样时）：** 在 `registry.PROVIDER_TYPES` 里加一项，然后实现一个与 `OpenAICompatClient.chat` 签名相同的客户端，在 `models.make_client` 里分派。
- **加事件：** 调用 `Agent._emit(type, **data)`，在终端渲染器里加一个 `on_<type>` 方法，在 `frontend/src/timeline.ts` 的 `buildTimeline` 里加一个 case。如果只是流式片段，要把它加进 `store.STREAMING_EVENTS` 和 `server/hub.DELTA_TYPES`。
- **改界面：** 改 `frontend/src` 里的代码后，运行 `npm run build`，把 `src/localagent/server/static` 一起提交。开发时先运行 `la ui --browser --no-open --port 8765`，再运行 `npm run dev`，Vite 会把请求代理到这个端口，页面地址里要带上 `#token=`。
- **改提示词：** 只改 `agent/prompts.py`。改动会让所有项目的前缀缓存失效一次，这没关系，但不要引入每次都会变化的内容。

## 9. 测试
- **离线：** `.venv\Scripts\python -m pytest`，目前 353 个测试，在 Windows 和 Linux 上都能全过。worktree 相关的测试都用临时目录里的真实 git 仓库。
  - 测试里不要写死 `python` 命令（很多 Linux 服务器只有 `python3`），用 `sys.executable`；也不要依赖 Windows 的中文代码页。
  - 模型一侧用 `tests/scripted_server.py`：它是一个真实的 HTTP 服务，按脚本返回流式响应，并把收到的请求记下来。
  - 工具、git、数据库、Bash 都用真实的临时目录。
  - 按 KISS 的原则，**不用 mock**。
- **在线：** `.venv\Scripts\python -m pytest -m live`，或者运行 `scripts/demo_fix_bug.py`。两者都用 `$LOCALAGENT_HOME`（默认检出目录下的 `data/`）里的注册表和角色。加 `--context-budget 5500` 可以强制交接；加 `--worktree` 会走 chat 分支和合并的流程。
- **步数：** `tests/test_agent_loop.py` 里验证被掐断的一步不吃步数（`step_start` 只发 1、2 两次）、步数用完时 summary 里带着模型写的笔记。
- **采样：** `tests/test_openai_compat.py` 验证 `extra_params` 进了每个请求、关思考时 `thinking_off` 覆盖其中几个而其余保持不变。
- **后台进程：** `tests/test_shell_tool.py` 里一条命令用 `nohup ... &` 起一个每 50 ms 写一笔的进程，确认它活过了命令本身，`background.stop()` 之后不再写；`tests/test_chat.py` 里走完整的 ChatSession：模型起了服务就 finish，任务结束后服务停了、并且记下了 notice。
- **思考上限和打转：** `tests/test_thinking_guard.py` 测判定本身（token 上限会停、中文照样按 token 算、一个 chunk 塞很多 token 时按字数兜底、没配上限就只靠打转判定、重复能认出、内容一直在变的长思考不误判、重复周期和窗口不对齐也能认出、重试时清零），`tests/test_agent_loop.py` 里有端到端：脚本服务器一直说下去或一直重复同一段，agent 在上限处断流、输出预算不动、下一次只给一半思考，第二次才关思考。
- **`nul`：** `tests/test_shell_tool.py` 真跑 `2>nul`、`>NUL 2>&1`，目录里不留 `nul`、结果里有说明且 exit code 仍在最后，只改重定向目标；`tests/test_worktree.py` 用 bash 真造一个 `nul`，提交照常成功、`nul` 不进提交，丢弃时整个 worktree 删得干净（没修之前这里的 `git add -A` 报 short read）。
- **计划：** `tests/test_plan.py`：模型的各种写法都能读成计划；Plan 调用的参数留在后面每一步的请求里、`plan` 事件的 id 对得上调用；只读 agent 没有 Plan、提示词还是提纲那条；交接后新会话的第一条消息里有"Plan so far"；思考被掐断的提示语让它用 Plan 记下来；没做完的任务把计划交给同一 chat 的下一个任务，做完的不交。
- **删 chat：** `tests/test_server.py` 里验证：分支上还有提交时拒绝（409，消息里有分支名），加 `discard_branch` 才连分支一起删，chat、任务、事件和待处理分支都消失。
- **存放位置：** `tests/test_config_home.py` 测 `data/` 的推断、旧 `~/.localagent` 的整体搬迁、搬不动时继续用旧的、`LOCALAGENT_HOME` 优先，以及 worktree 副本用原仓库的 data。
- **资源管理器和编辑区的数据：** `tests/test_files_api.py` 用真实 git 仓库测最近的文件夹、目录树的状态（改过、新文件、整个未跟踪文件夹、被忽略、已删除）、文件内容、diff 和基准时的内容（含 GBK 文件）、越出根的路径、chat 的 worktree。
- **服务端：** `tests/test_server.py` 用 FastAPI TestClient 测真实的 HTTP 和 WebSocket，覆盖 token、运行任务、审批、忙碌时拒绝新任务、插话、停止、中途连上的客户端、审阅和合并，以及附件上传/取回和整套注册表接口（密钥写进 secrets.env、不出现在任何响应里）。
- **附件和换模型：** `tests/test_attachments.py`（真的生成 PNG 和 PDF，检查抽文本、放进工作区、git 不可见）、`tests/test_set_model.py`（切换、拒绝装不下的模型、失败退回、图片只发给能看图的模型、Gemini 的 thought signature）。
- **界面：** 用 `la ui --browser --no-open` 启动后，在浏览器里操作一个真实任务：运行、插话、完成、看变更、合并、回放；再用一个脚本打开 pywebview 窗口，确认 WebView2 渲染正常、选文件夹的桥接可用、服务能正常退出。
