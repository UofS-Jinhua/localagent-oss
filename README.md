# LocalAgent

面向本地 LLM 的编码代理（类似 Claude Code / Codex），参考 [KISS Sorcar](https://github.com/ksenxx/kiss_ai) 的分层架构。
主要为 LM Studio、Ollama、vLLM、llama.cpp 这类 OpenAI 兼容的本地服务设计，以后可以混用云端 API。

- **架构（项目地图）：** [docs/architecture.md](docs/architecture.md)，内容包括分层、模块、数据流、不变量，以及与 KISS 的对比
- **设计与路线图：** [docs/design.md](docs/design.md)
- **KISS Sorcar 架构参考：** [docs/kiss-sorcar-notes.md](docs/kiss-sorcar-notes.md)

## 界面改成 VS Code 的样子

窗口按 VS Code 的布局重新排过：颜色用它的 Dark Modern / Light Modern，图标用它自己的 Codicons。

```
┌ 标题栏：项目菜单            [ 命令中心 · Ctrl+P ]            侧栏 / 编辑区开关 ┐
│活动栏│ 侧栏：会话 / 资源管理器 / 源代码管理 │   对话   │ 编辑区：文件和 diff 的标签 │
└ 状态栏：机器 · 分支 · 运行计时                 步数 · 审查 · 云端 · 思考 · 模型 ┘
```

- **活动栏（最左边一列图标）：** 会话、资源管理器、源代码管理，底部的齿轮里是命令面板、模型、机器、颜色主题。再点一次当前的图标就收起侧栏（`Ctrl+B` 也行）。源代码管理的图标上显示改动数。
- **资源管理器：** 文件树带文件类型图标和缩进线，文件名按 git 状态上色：改过的黄色、新文件绿色、删掉的红色并划线，被忽略的变灰。单击打开的是预览标签（斜体，下一次单击会替换掉它），双击才固定下来。
- **源代码管理：** 选中的 chat 有自己的分支时，最上面是这个分支，带合并 / 保留 / 丢弃三个按钮（原来右侧 Changes 面板里的那三个），下面是它改过的文件和它的提交。再往下是你自己检出里还没提交的改动。点一个文件，编辑区打开它的 diff；分支标题右边的按钮打开这个分支的全部改动。
- **编辑区（对话右边）：** 文件和 diff 用 **Monaco**（VS Code 本身的编辑器内核）显示，只读。语法高亮、缩略图、折叠、`Ctrl+F` 查找、`Ctrl+G` 跳到某行都是现成的。
  - 打开文件时，行号旁边和 VS Code 一样标出改动：绿色是新增，蓝色是修改，红色小三角是删掉内容的位置。
  - diff 是左右对比，编辑区窄了会自动变成行内显示。右上角可以切换左右 / 行内，也能跳到上一处或下一处改动。
  - 可以同时开多个标签。面包屑显示路径，以及这是哪棵树：你的检出，还是某个 chat 的分支。
- **对话里的工具调用：** Read / Write / Edit 的卡片右边多了一个按钮。Read 的打开那个文件，Write / Edit 的打开它的 diff。
- **命令面板：** `Ctrl+P` 按文件名模糊搜索并打开文件（输入 `edgrp` 就能找到 `EditorGroup.tsx`）。`Ctrl+Shift+P`（或 F1，或在框里先打一个 `>`）列出全部命令：新建 chat、换模型、开关思考和审查、设步数、打开文件夹、换机器、换颜色主题等。
- **状态栏：** 左下角的蓝色按钮是机器，相当于 VS Code 的远程指示器，点开可以选 This Computer 或某台服务器。旁边依次是当前分支（有未提交的改动时带 `*`）、chat 的分支、正在跑的任务和它的计时。右边是步数、审查、云端、思考和模型，点一下就切换。模型点开是分组的列表，已加载的模型还是呼吸的绿点。
- **可以拖：** 拖动分隔条调整侧栏和编辑区的宽度，双击分隔条恢复默认。宽度、侧栏开关、当前视图、diff 左右还是行内、颜色主题都记在服务器端的设置里，下次启动保持不变。
- **体积：** 首屏的 JS 基本没变（407 KB → 443 KB）。Monaco 单独打成一块（约 3.4 MB），第一次打开文件时才加载，全部来自本机，不走网络。只取了它的编辑器和各语言的语法着色；TypeScript、JSON 这些语言服务没带，它们各要一个 worker，还会再大好几 MB。
- **后端的配合：** `/api/file` 多返回一个 `base_text`，也就是这个文件在对比基准（HEAD，或 chat 开始的地方）时的内容，左右对比靠它。内容按字节读出再识别编码，所以 GBK 的文件左右两边都不会乱码。
- **第三方：** Monaco 是 MIT 许可。Codicons 的图标按 CC BY 4.0 发布，对外分发时要注明出处。

## 清理已删除项目的 chat

项目文件夹删了以后，它的 chat 在界面上打不开了，记录却一直留在数据库里。现在顶栏的项目菜单里，有这种 chat 时最下面会多一项 **"Clean up chats of deleted folders (N)"**。点开是个确认框：按文件夹列出，默认全选，可以取消勾选，确认后才删。

- **不会自动删。** "文件夹不在"和"文件夹暂时够不着"看起来一模一样：服务器上的网络家目录暂时读不到、移动硬盘拔了、网络盘断了、文件夹改了名，都会让它看起来"没了"。自动删会把还有用的记录悄悄删掉，所以由你来勾。确认框里也写了：只是暂时够不着的别删，文件夹回来 chat 就回来。
- **删的只是记录**：对话记录（任务和事件）、这个文件夹在"最近的项目"里的记录和它的模型设置，以及只被这些 chat 用过的附件。git 分支不动。
- 服务端删之前会再查一次，文件夹还在的、正在跑的 chat 一律不删。

**删 chat 时，附件副本也一起拿走。** 你发的附件会拷一份进工作文件夹的 `.la-attachments/` 给模型读。git 仓库里它在 chat 自己的工作副本里，丢弃时本来就跟着删；但不是 git 仓库的文件夹（还有 scratch）里，以前它会一直留在你的项目里。现在删 chat 时，会把它放进去、而同一文件夹里别的 chat 没用到的副本删掉；目录空了（只剩它自己的 `.gitignore`）就整个删掉，项目回到原样。两个 chat 共用的附件，删一个不会拿走另一个的。

## 修了：命令里的 `2>nul`

清理测试项目时发现一个删不掉的文件，名字就叫 `nul`。是模型在命令里写了 CMD 的习惯写法 `where msedge 2>nul`：Bash 跑在 Git Bash 里，`nul` 在那里只是个文件名，于是项目里多了个 `nul` 文件。Windows 把这个名字当设备，所以：

- 资源管理器和回收站删不掉它，丢弃 chat 时整个 worktree 目录也删不干净；
- **任务结束时的提交会直接失败**：git 读它读到的是空设备，`git add -A` 报 `short read while indexing nul` 然后退出。

现在是三道防线：

- 命令运行前，把引号外的 `2>nul`、`>NUL 2>&1`、`&>nul` 这类改成 `/dev/null`，并在结果里告诉模型"bash 里 nul 是文件名"，免得它下次还这么写；
- 提交时排除名叫 `nul` 的文件，万一有也不卡提交；
- 删目录时在 Windows 上走 `\\?\` 路径，这种文件也删得掉。

## 计划放进工具：Plan

翻了之前 5 次"体素场景"任务的记录：第一步都在思考里把整个项目设计了一遍（24K–103K 字符，5–17 分钟，有一次在思考里写了 246 行代码），然后第一个动作是 `ls`，或者干脆撞上限什么也没调。**更糟的是后面几步又从头设计**：其中一次第 3–6 步每步又想了 45–49K 字符，7 步只写出一个文件。原因是**思考不会发回给模型**，第一步想好的方案，第二步就看不到了。提示词里早就写着"多文件先写个提纲、思考每步之后会丢"，模型照样不写。KISS 的论文也只是把规划写成提示词规则（§6.1：3 个以上文件先列出每个文件改什么、顺序、怎么验证），它跑的是 Claude、GPT 这类前沿模型，没遇到这个问题。

所以加了一个 **`Plan` 工具**：步骤按顺序、各带状态，外加 `notes` 记后面要用到的设计决定。它的参数是对话的一部分，后面每一步都看得到。

- **会话交接后**，新会话的第一条消息里带着"Plan so far"。
- **任务没做完就结束时**（出错、停止、步数用完），计划存进数据库，同一个 chat 里你说"再试一次"，它从这份计划接着干。
- **界面上**把 Plan 调用画成清单：✓ 做完 / ▸ 正在做 / ○ 没开始，notes 点开看。
- **思考被掐断时**，提示语改成"用 Plan 记下已经想定的部分"：被掐断的思考整段丢掉，只有进了工具调用的才留得下来。
- 只有能改文件的 agent 有这个工具；审查者和只读子 agent 不变。不设审批，要改计划就插话。

**对比实测**（2026-09-21，A100 上的 Qwen3.8 27B，同一个体素任务，改之前的代码 A 和改之后的代码 B 各跑两次，每次 6 步，四个同时跑）：

| | 思考被截断 | 被截断白烧的思考 | 第一个文件 | 6 步用时 |
|---|---|---|---|---|
| A1（旧） | 4 次 | 144K / 共 146K 字符 | 没写出来 | 27 分钟 |
| A2（旧） | 4 次 | 146K / 共 192K | 18.4 分钟 | 32 分钟 |
| B1（新） | 1 次 | 48K / 共 82K | 15.1 分钟 | 17 分钟 |
| B2（新） | 1 次 | 44K / 共 57K | 10.6 分钟 | 12 分钟 |

- **第一步之后的差别很清楚。** 旧代码 `ls` 完又从头设计，每个 run 又撞了 3 次上限；新代码第 3 步记下计划（B1 列了 9 步，B2 列了 5 步），之后写文件每步几乎不再想。
- **第一步没变。** 四次都一样：想满 13,700 token（6–8.5 分钟）被截断，重试时调的都是 `ls`。Plan 管不到第一步，这要另想办法。
- **notes 两次都是空的。** 模型只列了步骤（B1 的步骤写得挺细，连算法都写进步骤里了），没单独记设计决定。
- 样本只有 2 对 2，6 步只写到骨架；写到复杂文件时会不会又重想，要跑得更长才看得出来。

**再跑长一点：20 步**（同样的任务和机器，旧代码 1 次、新代码 2 次，三个同时跑）：

| | 思考被截断 | 被截断白烧的思考 | 第一个文件 | 20 步用时 | 20 步后到了哪 |
|---|---|---|---|---|---|
| A1（旧） | 8 次 | 267K / 共 332K 字符 | 18.3 分钟 | 53 分钟 | 源文件都写完了，还没跑起来验证 |
| B1（新） | 2 次 | 82K / 共 174K | 12.0 分钟 | 37 分钟 | 还差 `main.js` 没写 |
| B2（新） | 1 次 | 24K / 共 61K | 7.6 分钟 | 15 分钟 | 整个场景写完，dev server 起来了，正在看无头截图调参数，差 README |

- **写复杂文件时没有再从头设计。** B2 记下计划后的 17 步，每步思考都不超过 2K 字符；B1 写大文件前还会想一阵，第 4 步写 `world.js` 之前撞了一次上限，重试就直接写出了文件，不是整个项目重想。旧代码第 2–6 步和第 11 步都撞了上限。
- **每步的进度不一定更多。** B1 把项目拆得更细，还写了一个验证脚本，20 步后反而比 A1 少写一个文件；但它少用了 16 分钟。
- **计划从来没更新过。** 三个新代码的 run 都只调了一次 Plan，清单上一直是"0 步完成"。工具说明里要求做完一步就再调一次，模型没照做，所以交接和"再试一次"带过去的是一份进度过时的计划。

试过**在每步工具结果末尾的状态行里带上计划进度**：`[step 12/20 · context 13,400/262,144 tokens · plan 2/7 done]`，想让它做完了却还显示 0 时自己去改。又跑了一次 20 步：第 2 步记下 7 步计划，之后 18 步一次没更新，计数一直是 0/7 —— **没用**。

所以反过来，**不再指望计划里的状态**：

- 带进新会话的"Plan so far"、带进下一个任务的"Plan N when it stopped"，都写明"状态可能过时，以交接笔记和文件为准"。交接笔记里的进度本来就是准的。
- 界面上的计划卡片只写"7 steps"，不写"0/7 done"。清单就是记录那一刻的样子，它本来就放在记录它的那一步里。
- 状态行里的计数留着：只多几个 token，模型偶尔标了也有用。

计划真正起作用的是那份设计和步骤划分（上面的对比里省下的都是这个），不是那些勾。

## 换成采样之后：循环没了，步数不够了

同一个体素任务又跑了一遍（LM Studio 上的 Qwen3.8 27B，IQ3_S，100 步 / 4 小时 14 分）。对着上一跑的基线：

| | 上次（贪心解码） | 这次（Qwen 采样 + 新提示词） |
|---|---|---|
| 思考打转 | 有，30,055 字符处被判定 | **一次都没有** |
| 思考超限被掐 | 7 次（都在前 12 步） | 5 次（都在前 9 步） |
| 白烧的思考 | 122,030 字符 | 192,010 字符 / 39 分钟 |
| 首次工具调用 | 255 秒 | 559 秒 |
| **有产出的那些步里，最长的思考** | 22,424 字符 | **43,142 字符** |
| 结果 | 56 步完成 | 100 步用尽（95 步有工具调用） |

两件事要挑明：

**48,000 这个上限是对的，别再往下调。** 这次有 **7 个产出了工具调用的步**思考超过 24,000 字符，最长的 43,142 字符（第 15 步，之后正常写文件）。要是按最初考虑的设成 24,000，砍掉的就是这 7 步真活。代价是"白烧"的字符数反而涨了 —— 卡住的一步现在要烧满 48,000 再减半到 24,000，前 9 步花了 39 分钟。剩下 91 步一次都没再超限。

**步数用完不是空转。** 100 步里 95 步都在调工具（Write 34、Edit 24、Bash 26、Read 22），第 73 步还因为上下文满了交接了一次。就是活没干完。所以改了三处：

- **步数上限挪到顶栏可调**（"steps"，跟 thinking / cloud / review 一样记在服务器端设置里）。一个项目要几步只有你知道。
- **被掐断后的重试不再占步数**：那是同一步再来一次，界面上的"第 N 步"和模型看到的 `[step N/100]` 不该数它没做成的事。
- **步数用完时也写一份笔记**（和上下文满时交接用的是同一套），放进 summary。以前只留一句"Stopped after reaching the step limit (100)"，几小时的上下文全丢；现在在同一个 chat 里发下一条消息就能接着干。

## 采样参数：我们一个都没发，而服务器在用贪心解码

之前我们**一个采样参数都不发**（`temperature` / `top_k` / `top_p` 全靠服务器默认）。实测 LM Studio（2026-09-19，Qwen3.8 27B）：

| 发什么 | 同一请求跑 4 次 |
|---|---|
| 什么都不发（LocalAgent 以前的行为） | **逐字相同** |
| `temperature=0`（显式贪心） | 逐字相同 |
| `temperature=0.8, top_k=40` | 有变化 |
| Qwen 思考档 `t=1.0 p=0.95 k=20 min_p=0` | 变化更大 |

也就是说我们一直在**贪心解码**下跑。贪心正是思考模型无限重复的经典成因 —— 那次 10,800 字符一个周期的**逐字**循环，在采样解码下概率低到可以忽略，在贪心下却是必然的：状态一旦回到原处，后面每个 token 都会一模一样。之前做的"发现打转就掐断"因此是在治症状，病根是这个。

**不需要加新字段。** 模型本来就有两个自由字典，正好对上 Qwen 的两档设置：

- `extra_params` —— 每个请求都发：`{"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0}`
- `thinking_off` —— 关思考时再盖一层：`{"reasoning_effort": "none", "temperature": 0.7, "top_p": 0.8, "presence_penalty": 1.5}`

字典里**没有的键就不发**，所以"这一项用服务器默认"天然成立，不用发明 `-1` / `-inf` 那种哨兵 —— 而且 `temperature` 和 `min_p` 的 0 都是有意义的真值，用哨兵反而会踩坑。两个字典现在都能在模型设置里直接编辑（Models → 改 → "request params"）。

不做成 temperature、top_k 这样一格一格的输入框，是因为 `top_k`、`min_p`、`repetition_penalty` 都不在 OpenAI 规范里，各家名字还不一样（`repeat_penalty` / `repetition_penalty`），云端模型收到还会报错；自由字典这三件事都能办。**一个坑**：LM Studio 对不认识的键是静默忽略的（实测），名字写错不会报错，只会没效果。

Qwen 的模型照上表这两档配就行。注意 Qwen 的思考档里 `repetition_penalty=1.0`、`presence_penalty=0.0` —— 思考时**不加**重复惩罚，惩罚只在不思考那档；所以也没有另加 `frequency_penalty`。

## 下一步明确就执行

提示词要求：下一项工具调用及参数确定后立即执行，不继续推演后续文件的完整实现；短计划与第一项操作放在同一回复，新项目先建可运行骨架再逐步加功能。

思考保护维持原来的逐步重试行为：一次工具调用后恢复模型配置的额度。撤回跨步骤持续缩减额度和连续三个工具回复关闭思考的策略，避免早期一次循环压缩后续正常编码的思考空间。

## 一次跑完之后：思考额度放宽、计划要写下来、跑完停进程

那个体素场景的任务这次 56 步跑完了（50 分钟，成功）。翻记录之后改了三件事。

**1. 思考上限从 24,000 提到 48,000 字符。** 24,000 确实会截断：那一步写 `main.js` 的思考用了 **22,424 字符**（6,100 tokens，随后写出 12,941 tokens 的文件），离上限只差 1,576。实测这些模型是 **3.1–3.9 字符 = 1 token**（英文思考），所以 48,000 ≈ 13K tokens，在 10–16K 这个区间里，也是那次有产出的最长思考的两倍多。9B 那个（32K 窗口）给 24,000。

代价要说清楚：卡住的一步现在要烧到约 13K tokens 才被掐（24 tok/s 下约 9 分钟），之前是 4 分钟。如果前几步还是原地打转，可以退到 32,000（约 9K tokens，对 22,424 字符仍有 40% 余量）。

**2. 计划必须写下来 —— 这才是"想一遍又一遍"的原因。** 记录里第 **1、3、6、10** 步各把思考预算烧光一次，中间夹着的步骤只是写一个文件；第 1 步烧完 24,000 又烧 12,000，关了思考之后干的事是 `ls -la && pwd`。原因不是模型笨：**思考不会进对话**（只有回复正文和工具调用会留下），所以它第 1 步想清楚的整套方案，到第 3 步真的不存在了，只能从头再推一遍。

提示词里因此加了两条：先看项目再决定下一步，不要在没看过之前把整套方案设计完；三个文件以上的活，把计划**写进回复正文或一个文件**，并说明"你的思考每步之后就被丢掉了"。第 9 步之后它就正常了 —— 那时项目里已经有文件可看，不必再凭空想。

**3. 任务结束停掉命令留下的进程。** 那次它起了 `nohup npm run start -- --port 5173 &`，收尾时写的是 `pkill -f "vite --port 5173"` —— Git Bash 上根本没打中，服务一直占着 5173。现在每条命令的进程组（Windows 上是一个 job object）都记在任务名下，任务结束时统一停掉，停到了就在对话里说一声。**命令返回时不停**：下一步要 curl 上一步起的服务。提示词里也加了"结束前把你为测试起的服务停掉"。

另外，这次**没有用过 sub agent**（Task 工具一直在，它一次没调）。后面那些"子任务"是它自己一步步做的。这次也不需要：上下文最多只用到 26,935 / 122,880，而 sub agent 的价值是隔离上下文；这次缺的不是上下文，是"别再重想一遍"。

## 思考单独限长：输出预算归输出，思考归思考

上一版只做了"打转就断流"，那**治不了根**：一个山水场景的测试项目里，第一步在 30,055 字符处被判打转停下，重试又转了一遍。

翻那次记录才看清两件事：

1. **判定不可能更早。** 模型是在 11,609 字符处把前面**整段计划原样重写了一遍**，一个循环周期 10,800 字符。任何逐字比对的办法都要等第二遍走完才看得见 —— 实测最早也要 22,625 字符。所以打转判定只是保险丝，不是闸门。
2. **闸门本该是上限，但那个上限太大，而且大得有道理。** 之前把上限抬到 40K/60K，是因为**写文件**的步骤确实会用掉 16,384 tokens（llama-server 上那次写页面就是）。一个数同时管"写文件"和"想问题"，就只能在写不完文件和想上半小时之间二选一。

所以现在给思考单独一个上限 `max_thinking_tokens`（模型设置里可改）：流式阶段数 token，超了就断流，**输出预算一动不动**。服务器要到最后那个 chunk 才报 token 数，所以是边收边数：llama.cpp 和 LM Studio 每个 token 发一个 chunk（实测和服务器自己的计数对得上，开着 MTP 也一样），一个 chunk 就算一个 token；一个 chunk 塞好几个 token 的云端服务，按至少 6 字符 1 token 兜底。这个上限原先是按字符数的，但中文每个 token 只有约 1.15 个字符，同一个 48,000 在英文是 13K tokens、在中文是 42K，所以改成了 token；旧配置里的 `max_thinking_chars` 读进来时按 3.5 字符 = 1 token 换算。

27B 模型现在的设置：输出 24,576 tokens（够写一个大文件）、思考 13,700 tokens（原来的 48,000 字符）、第一步不再加倍 —— 第一步之所以要 60K，本来就是被思考撑起来的。

降级阶梯也跟着改：同一步重试时**只把思考减半**（没配上限的模型，就按它这次实际想了多少的一半来，每一级都更紧），输出预算只有在真的被输出上限截断时才减半；第二次失败才关思考；第三次才算失败，并说该调哪个数。

## 删除 chat；第一步想得久一点，后面收紧

- **删除 chat：** 侧栏里每个 chat 悬停时右上角有一个垃圾桶。删掉的是这个 chat 的全部记录（任务、事件、子 agent 的运行记录）。
  - **它的分支不会被顺手丢掉**：`la/<chat>` 上还有提交、或者 worktree 里还有没提交的改动时，第一次会被拒绝并告诉你留着什么，确认第二次才连分支和 worktree 一起删。记录很容易重来，改动不是。什么都没有的空分支直接跟着删，不会多问一句。
  - 正在跑的 chat 不能删（按钮不出现，接口也会拒绝）。附件不删：它们按内容寻址、被多个 chat 共用。
- **每步的输出预算分两档：** 会话的**第一步**要读任务、看项目、定方案，思考天然最长；后面的步骤是执行，不该也不需要那么长。所以模型多了一个 `max_output_tokens_first`（模型设置里的 "first step output tokens"，0 表示和后面一样）。
  - （这两个数后来都降了下来，第一步的加倍也去掉了：见上面「思考单独限长」一节。）
- **被截断时改成逐级降级**（之前是一步到位关掉思考，那一步质量明显变差）：
  1. 第一次截断：**同一步用一半预算再想一次**，对话里带着"你被截断了，想短一点，现在就调工具"。
  2. 第二次截断：这才关掉思考重试。
  3. 第三次：失败，并说明是把输出上限用在思考上了。

## 一处存放：`data/`；思考被截断不再悄悄卡住

- **程序自己的文件都在检出目录的 `data/` 下**：`models.yaml`、`secrets.env`、`localagent.db`、`attachments/`、`scratch/`、`remotes.yaml`、`web.yaml`、`skills/`。以前在 `~/.localagent`，现在一个文件夹就是一整套安装，备份和搬机器都只拷这一份。
  - 第一次运行时，旧的 `~/.localagent` 会被**整个 rename 过来**（要么全部成功，要么原样不动：有别的 `la` 进程占着数据库时移不动，那就继续用旧的，绝不会一半在这边一半在那边）。
  - `data/` 已经写进 `.gitignore`，所以 `secrets.env` 不会被提交。
  - `LOCALAGENT_HOME` 照旧可以覆盖（服务器上家目录用不了时就是这么办的）；装成 wheel（没有源码树）时仍然是 `~/.localagent`。
- **顶栏的开关也记住了**：thinking / cloud / review 和侧栏标签存在数据库里，**模型按项目分别记**（A 项目用本地 27B、B 项目用云端，不用每次改）。和"最近的项目"同一个原因：桌面窗口每次启动换端口，网页自己的 localStorage 下次读不回来。
- **任务计时**：每条任务下面写着开始时间、结束时间和总耗时，运行中每秒跳一次。刷新页面也不会从零开始 —— 开始时间由服务器给。
- **输入框随内容变高**：任务描述长了自动变高（最多占窗口高度的 40%），不用再滚上去看自己写了什么。

### 模型把输出上限用光时，现在会说出来

实测（本地一个 27B 的社区微调，122K 窗口）：8 步里有 6 步 `completion_tokens` 正好等于 `max_output_tokens = 16384`，也就是**思考还没结束就被输出上限砍断**，那一步既没有文字也没有工具调用。界面上只显示"thought 56,370 characters"，每步约 8 分钟，49 分钟后以"模型不调工具"失败，看不出到底哪里不对。现在：

- 发生时立刻在对话里写明：`the reply hit <模型>'s output limit of 16,384 tokens while still thinking, so it called no tool.`
- **下一步自动关掉思考重试**（模型支持 toggle 时），通常一步就恢复正常。
- 连着几步都这样才失败，而且失败原因直接告诉你该怎么办：调大这个模型的 `max_output_tokens`，或者关掉 thinking。

## 界面：文件和 diff、最近的项目、滚动和复制

（下面说的 Files 标签和右侧的文件查看器，后来换成了 VS Code 式的资源管理器、源代码管理和编辑区，见最上面一节。）

- **模型下拉框：** 服务器上**已经加载**的模型前面有一个呼吸的绿点，没加载的是空心圆（选它会让服务器换模型），云端模型是云朵图标。列表按"已加载 / 本地未加载 / 云端"分组，每次打开都重新问一次服务器。
- **最近的项目：** 点顶栏的文件夹按钮，列出最近用过的项目（最新的在前；鼠标移上去可以从列表里去掉，chat 不受影响），下面是"打开其他文件夹"和 scratch。**不带 `-w` 启动时直接打开上次的项目**，双击快捷方式就是这种。
  - 这个列表存在服务器的数据库里，不在网页的 localStorage 里：桌面窗口每次启动端口都不一样，localStorage 是按端口分开的，存了下次也读不回来。以前"记住上次的文件夹"就是因为这个没生效；而且 `la ui` 不带 `-w` 时默认用当前目录，快捷方式打开的永远是 LocalAgent 自己的文件夹。
- **侧栏的 Files 标签：** 当前项目的文件夹和文件，一层一层展开，旁边标出 git 状态：`M` 改过、`U` 新文件、`D` 删掉了（删掉的文件照样列出来，划一道线），被 .gitignore 忽略的变淡。"Changed" 只列有改动的文件。
  - 选中一个有自己分支的 chat 时，默认看**这个 chat 的 worktree**，改动相对这个 chat 开始的地方（提交了的也算）；可以切回"Your checkout"，改动相对 HEAD。
- **看文件和 diff：** 点一个文件，右侧打开它。File 是带行号的全文，新增和改过的行标绿；Diff 是这个文件的 `git diff`（还没进 git 的新文件显示成全部新增）。有改动的文件默认先打开 Diff，图片直接显示。只读，不会改任何东西。
- **自由滚动：** 任务运行时，只有停在最底部才跟着输出往下走；往上滚就停在你看的地方，不会再被拽回去。右下角三个按钮：回到 chat 顶部、回到最新一轮的开头（你最后发的那条消息）、回到底部并重新跟随（有新输出时这个按钮会亮）。
- **复制：** 桌面窗口以前**根本选不中文字**：pywebview 默认把页面设成禁止选择。现在可以直接选中复制。消息、回复、代码块、工具调用和它的输出、思考过程也都有复制按钮（鼠标移上去才出现）。
- **新图标：** 代码括号中间一颗星。`scripts/make_icon.py` 同时生成快捷方式用的 `assets/localagent.ico` 和网页用的 `icon.png`，窗口、浏览器标签页、顶栏都换成了它。快捷方式直接引用那个 .ico，拉取之后就是新图标；如果 Windows 还显示旧的，那是图标缓存，重新跑一次 `setup.cmd` 就会刷新。

## M10：联网搜索、skill 同步到服务器

- **WebSearch / WebFetch：** 模型拿不准的东西（库的新版本和参数、报错信息、论文）先搜再读，不靠记忆猜。
  - `WebSearch(query)` 返回标题、网址、摘要；`WebFetch(url)` 把网页读成纯文本：去掉脚本、导航栏、页脚，页面标了 `<article>`/`<main>` 就只取正文，链接保留成 `[文字](网址)` 方便继续点。
  - 一次最多给 1 万字，长页面用 `start` 接着读，同一页不会重复下载。PDF（比如 arXiv 论文）直接给抽出的文字。
  - 只读的子 agent 和审查者也有这两个工具：它们不改文件。读很多网页的调研适合交给子 agent，主 agent 只拿回报告。
- **搜索用哪家：** 界面齿轮里的 **Web** 一节（或 `data/web.yaml`），可以点"Try a search"试一下。
  - `duckduckgo`（默认）：不用 key、不用注册。它读 DuckDuckGo 的简版网页，如果被当成机器人拦下，工具会报错并提示换一家。
  - `searxng`：自己架的 SearXNG（要在它的 settings.yml 里打开 json 格式），填地址。
  - `brave` / `tavily`：要 API key，和模型的 key 一样存在 `secrets.env`，agent 执行的命令读不到。
  - `off`：只保留 WebFetch；把整个"web tools"关掉则两个工具都没有（适合不能上外网的机器）。
  - 改了设置从下一个任务起生效。工具列表变了，前缀缓存会重算一次，之后照常命中。
- **命令行：** `la web show`、`la web set --search brave`（key 用 `la secret set BRAVE_API_KEY`）、`la web search 投机解码`、`la web fetch <网址>`，看到的和模型看到的一样。
- **每台机器各有一份设置：** 在服务器上跑的任务用服务器自己的 `web.yaml` 去联网。
- **在服务器上自己跑一个 SearXNG（推荐）：** 共享服务器、云主机的出口 IP 常被搜索引擎当成机器人（2026-09-15 实测，从一台服务器访问 DuckDuckGo 简版页被拦，返回 202）。SearXNG 是一个开源的"元搜索"，替你去问 Google、Brave 等，只给你一个人用时不容易被拦。不需要 root 和 docker（下面的 `<base>` 是服务器上你放东西的目录）：

```bash
ssh <服务器> mkdir -p <base>/searxng && scp scripts/gpu-server/*searxng.sh <服务器>:<base>/searxng/
```

  然后在服务器上：

```bash
cd <base>/searxng && ./setup-searxng.sh && ./run-searxng.sh
```

```bash
la web set --search searxng --url http://127.0.0.1:18888
```

  - `setup-searxng.sh` 下载源码包（服务器上 git clone GitHub 很慢）、用 uv 建虚拟环境、生成只监听 127.0.0.1:18888、打开 json 格式的 `settings.yml`。再运行一次就是更新（`settings.yml` 保留）。需要 `uv`，不在 PATH 上时用 `UV=/path/to/uv ./setup-searxng.sh`。
  - `run-searxng.sh` 在 tmux 会话 `la-search` 里启动；平时用下面的 `la-services` 一起管，不用单独记。
  - 实测：一次查询约 0.7 秒（中文约 3 秒），结果来自 Google CSE 和 Brave；DuckDuckGo 出验证码，SearXNG 会自动跳过它。

- **Skill 同步：** 在界面里连上一台机器（Machines）后，本机 `data/skills/` 里的 skill 会自动拷到那台机器的 `data/synced-skills/<本机名>/`，所以在服务器上跑的任务也能用。连接期间每 5 秒比对一次，本机增、删、改 skill，那边几秒内跟上。Machines 表格里会显示"N skills copied"。
  - **只从本机拷到服务器，不会反过来。** skill 是模型照着做的指令，里面还可能有它会运行的脚本；服务器万一被入侵，不能借此把东西塞回你的笔记本。同样的原因，本机的界面 token 也从不交给服务器的页面。
  - **同名时优先级：** 项目里的 > 服务器自己的 `data/skills/` > 从本机同步来的。服务器自己的 skill 永远不会被同步覆盖或删掉。
  - 项目里的 skill（`<项目>/.localagent/skills/`）照旧跟着 git 走，不需要同步。
  - **服务器上的 LocalAgent 也要更新到这个版本**（见下面「把代码更新到服务器」），旧版本收不了，表格里会提示。

## 把代码更新到服务器

服务器上没有你的 GitHub 凭据（也不该放）时，那边 `git pull` 会卡在要用户名。反过来推就行 —— 走的是已经能用的 ssh（下面的 `gpu1` 是 `~/.ssh/config` 里那台服务器的别名，`<base>` 是你在那边放东西的目录）：

```bash
git push gpu1 main
```

一次性准备：本机 `git remote add gpu1 ssh://gpu1/<base>/LocalAgent`，服务器上 `git -C <base>/LocalAgent config receive.denyCurrentBranch updateInstead`（这一条让推送直接更新那边的工作区，前提是它干净）。

推完还要做的：

- **改了 `data/` 里的东西不会跟着走**（`data/` 不进 git）。服务器有自己的注册表（那边的 `data/models.yaml`，或者 `LOCALAGENT_HOME` 指向的目录），模型设置要在那边单独改一遍。
- **界面不会自己用上新代码**：`ssh gpu1 <base>/bin/la-services restart ui`。重启后 token 会变，app 里点一次 Connect 就好。
- 顺手在那边跑一遍测试，Linux 上的行为和 Windows 不一样的地方（进程组、路径）就是靠它兜底：`ssh gpu1 'cd <base>/LocalAgent && .venv/bin/python -m pytest -q'`。

## 服务器上的三个服务：一条命令管起来

GPU 服务器上跑着三样东西，各自一个 tmux 会话：`la-swap`（llama-swap，有请求才加载模型）、`la-search`（SearXNG）、`la-ui`（那边的界面）。`scripts/gpu-server/la-services.sh` 把它们合在一起管，拷到服务器的 `bin/` 下（`scp scripts/gpu-server/la-services.sh gpu1:<base>/bin/la-services`）：

```bash
ssh gpu1 <base>/bin/la-services status
```

（家目录在网络盘上、可能暂时读不到时，写绝对路径更稳。）

- 不写服务名就是三个都管，也可以只点名一个或几个：`la-services restart search`、`la-services stop ui swap`。
- `status` 显示每个服务在不在、端口是不是你自己在监听，以及此刻谁占着 GPU。端口通不通不算数：共享服务器上谁都能在 `127.0.0.1` 上监听。端口被别的用户占着时显示 `PORT HELD BY uid …`，退出码为 1；`start` 发现端口已经被占（不管是谁）就不启动。
- `stop` 先给会话里的所有进程发 TERM，等它们退出，再关 tmux 会话。只关会话不够：SearXNG 用的 granian 把关会话时的 SIGHUP 当成"重新加载"，以前每次 stop / restart 都留下一个还在监听的 SearXNG（2026-09-21 一次清掉了 6 个）。
- 停的顺序是界面 → 搜索 → 模型服务，启动反过来；已经在跑的不会被重复启动。
- **界面上有任务在跑时，`stop ui` 会拒绝**，要真想停就加 `--force`。
- 路径按脚本自己的位置推断（放在 `<base>/bin/` 下，就用 `<base>/localllm`、`<base>/searxng`、`<base>/bin/la-ui`），换台机器可以用 `LA_BASE=... la-services status`。
- 服务器重启后跑一次 `la-services start` 就全回来了。界面其实不用手动开：app 的 Machines 里点 Connect 会自己启动它。

### 不用 ssh 进去也能开关：机器上"有名字的命令"

`data/remotes.yaml` 里给一台机器写几条命令，它们就会出现在 Machines 对话框里，点一下就在那台机器上跑，输出显示在下面（也可以在对话框里的 commands 框里直接编辑，一行一条 `名字 = 命令`）：

```yaml
remotes:
  gpu1:
    host: gpu1
    forwards: ['18080', '8765']
    ui: /srv/you/bin/la-ui
    commands:
      start: /srv/you/bin/la-services start
      stop: /srv/you/bin/la-services stop
      status: /srv/you/bin/la-services status
      restart: /srv/you/bin/la-services restart
```

命令行是同一条路：

```bash
.venv/Scripts/la remote run gpu1 start
```

```bash
.venv/Scripts/la remote list
```

- **接口只收名字，不收命令行**：能跑的永远只有你写在 `remotes.yaml` 里的那几条，别人拿到界面 token 也变不出新的命令。
- 每条命令自己起一条短 ssh，跑完就退；要密码时照样在界面里弹框，命令行下则在终端里问。
- 超过 5 分钟没结束就会被停掉，输出太长会截中间。

### 让别的程序也能开关

不需要 LocalAgent，任何程序一行 ssh 就够——`la-services` 本身就是那个通用接口：

```bash
ssh gpu1 /srv/you/bin/la-services start
```

Python 里：`subprocess.run(["ssh", "gpu1", "/srv/you/bin/la-services", "start"], capture_output=True, text=True)`，退出码就是结果。脚本自己也能远程跑，省得两边都记路径：

```bash
LA_REMOTE_CMD=/srv/you/bin/la-services scripts/gpu-server/la-services.sh --host gpu1 status
```

前提是那台机器的 key 登录可用；无人值守的程序建议先 `ssh -o BatchMode=yes` 试一下，失败就提示人去登录一次。

## M8：Skill（按需打开的说明书）

一个 skill 就是一个文件夹，里面放一个 `SKILL.md`：开头的 YAML 写清楚**它叫什么、什么时候该用**，正文用 Markdown 写**这类活该怎么做**。文件夹里其它东西（脚本、模板、参考资料）就是普通文件，模型可以 Read，也可以用 Bash 跑。

- **放哪：**
  - `data/skills/<名字>/SKILL.md` —— 所有项目都能用
  - `<项目>/.localagent/skills/<名字>/SKILL.md` —— 只在这个项目，可以跟着仓库提交给同事
  - 同名时项目里的那个优先。
- **和 Claude Code 通用：** 用的就是它的 `SKILL.md` 格式，网上找到的 skill 一般直接放进去就能用。搬不过来的是引用了它专有工具（MCP、hook、`allowed-tools`）的部分，以及写给前沿模型的长篇大论——本地 27B 需要更短、更命令式的写法。没有 YAML 头也能读：用文件夹名当名字，第一行当描述。
- **为什么不是全塞进提示词：** 提示词前缀（系统提示 + 工具定义，约 2,300 token）必须一个字节都不变，本地服务的前缀缓存才会命中，而且每个 token 都要花 prefill。所以**只有目录进提示词**（每个 skill 一行、约 15 token，20 个也才多约 1.2 秒冷启动，之后一直缓存），**正文由模型自己用 `Skill(name)` 工具取**，作为工具结果追加进对话——前缀纹丝不动。
- **命令：**

```bash
.venv/Scripts/la skills list          # 这个目录能用哪些（模型看到的就是这几行）
```

  另外还有 `la skills show <名字>`（按模型看到的样子打印）、`la skills add <文件夹> [--project]`（拷一个进来）、`la skills remove <名字>`。界面里齿轮的最下面也列出当前文件夹可用的 skill。
- **实测（本地 qwen3.8-27b）：** 项目里放一个 `changelog` skill 规定 CHANGES.md 的写法，然后只说"记录一下：我们加了子 agent 功能"——模型第一步就调了 `Skill("changelog")`，读完按格式写进去，4 步 15 秒，格式完全符合。

## M7：子 agent（省上下文）、上下文监控

- **子 agent（`Task` 工具）：** 翻代码库、读长文件这种"输出会把上下文塞满"的活，交给一个有自己上下文的子 agent，主 agent 只拿回它的报告。**这是省上下文，不是并行** —— 实测在一张 2080 Ti 上两个请求并发的总吞吐反而略降，所以它是顺序跑的（主 agent 等着）。
  - 它的窗口 = 主 agent 剩下的额度，上限 32K，不足 8K 就直接拒绝：两段对话同时在共享 KV 里，挤掉主 agent 的前缀要重新 prefill（70K 大约 5 分钟），比省下来的贵得多。
  - 默认只读，不能再开子 agent。
  - **在支持并行的模型上可以同时跑**（模型设置里的 `max_parallel_subtasks`，A100 上的 Qwen 设为 3）：同一轮里派出的只读子 agent 一起跑、平分预算；可以写文件的子 agent 永远单独跑。本地 2080 Ti 上的模型保持一个接一个：那张卡并行不提高总吞吐。实测提醒：以读文件为主的子 agent 并行并**不会**更快（读就是 prefill，吃的是算力，三个一起跑各分三分之一），见 [design.md §1.6.2](docs/design.md)。
  - 它做的每一步都存成一条子任务记录（`la show` 会列出来）。要追溯先看 `git diff`，不够再翻记录。
  - 关掉：`la run --no-subtasks`。
- **上下文监控：** 对话上方有一条用量条，显示窗口用了多少、**这段会话会在哪里交接**、当前这步的首 token 时间和速度、本任务累计 token 和花费。`la show` 里每个任务也有这些数字。

## M6：审查者、@ 提及、看 PDF 里的图、不用项目也能聊

- **审查者（一个写、一个审）：** 顶栏打开 **review**（命令行 `--review`）。任务做完、**提交之前**，`reviewer` 角色的模型读这次改动的 diff，只给它只读工具（Read / Grep / Glob / RunTests），它没问题就说没问题，有问题就列清单交回主模型去修，**修完和原改动一起提交一个 commit**。`reviewer` 角色没单独配就用同一个模型的新上下文；配成云端模型就是真正的跨厂商互审。
  - **它自己跑测试，而且会判断测试本身有没有意义。** 摘要里写「测试都过了」不算证据，审查者必须用 RunTests 亲眼看到输出和真实退出码；把被测对象换成 stub、只断言「某个函数被调用过」、或者把改动还原回去照样能过的测试，都会被当成问题写进清单（真正的外部依赖——付费接口、远程主机、时钟——可以假造，被测代码本身不行）。它只能跑项目自己的测试命令：只读模式下 RunTests 没有「自定义命令」这个参数，否则那就是一个 shell，「审查者不能改代码」也就成了空话（实测里它正是用这个参数跑了 git 和 mkdir）。
- **@ 文件提及：** 输入框里打 `@` 直接选这个文件夹里的文件（和 agent 看到的是同一份清单，遵守 .gitignore），省掉"你指的是哪个文件"那一轮。
- **看 PDF 里的图：** 论文的图表不在抽出来的文字里。现在 agent 可以 `Read(paper.pdf, page_number=7)`，把那一页渲染成图片看（只对能看图的模型提示这个用法）。
- **没有项目也能用：** 首页有"用 scratch 文件夹"的入口（命令行 `la run --scratch`），在 `data/scratch` 里干活 —— 真实目录、工具照常可用，但永远不是 git 仓库、永远不做分支隔离。适合问个问题、写个一次性脚本。

**关于 git：** 打开的文件夹**不是** git 仓库时不会报错，也**不会**帮你 `git init`。它会提示 `working directly in …: not a git repository`，然后直接改文件。仓库还没有任何提交、HEAD 处于游离状态、或指定的 base 分支不存在时，同样退回这种直接工作模式。

## M6 之前：问用户、成本、稳健性

- **AskUser：** 遇到只有你能决定的事（指的是哪个文件、用哪个方案），agent 会停下来问你，界面弹窗、命令行等你敲一行；没人应答就自己决定并在总结里说明假设。只有"有人能回答"时才会注册这个工具，无人值守的运行不会卡住，每个任务最多问 5 次。
- **成本：** 每一步按**实际跑这步的模型**计价（换过模型也能正确累加），命令行和界面在不为零时显示；本地模型是 0，不会碍眼。价格在模型设置里填（每百万 token 多少美元）。
- **修掉的问题：**
  - 快捷方式启动时子进程乱弹控制台窗口（`pythonw` 没有控制台，每个 git 子进程就自己开一个）
  - `max_sessions=0` 直接崩溃
  - 长任务的事件积压无上限，新连上的界面要收全量
  - agent 能从自己的命令环境里读到模型 API 密钥
  - 附件上传失败会被静默丢掉
  - base 分支被删掉后合并抛出 git 异常（界面显示 500）
  - 流里出现一行坏 JSON 会终止整个任务

## M5：云端模型、任务中途换模型、图片和文档

- **接入云端：** 界面顶栏的齿轮里可以添加服务（LM Studio、Ollama、Gemini、OpenAI、Anthropic 兼容层、DeepSeek、OpenRouter、通义千问），探测它有哪些模型，按模型设置上下文长度、能不能看图、思考开关，指定角色，并做一次只花 1–2 次请求的测试。
  - **密钥不会进 `models.yaml`**，只存在 `data/secrets.env`（命令行用 `la secret set GEMINI_API_KEY`），而且读出来不会放进环境变量，所以 agent 执行的命令看不到它。
  - 命令行：`la init --preset gemini --only gemini-flash-latest`。
- **任务中途换模型（`set_model`）：** 给模型打上"允许 agent 切换"，它就能在任务中途换一个模型继续，对话原样带过去。典型用法：本地模型看不了图，临时换到能看图的模型看一眼截图，再换回本地改代码。云端模型要额外打开顶栏的 "cloud" 开关（默认关）。换过去的模型如果报错（密钥失效、没额度、连不上），会自动退回任务开始时的模型继续，任务不会因此失败。你也可以在任务运行时直接用顶栏的下拉框把它换到别的模型。
- **本地模型列表跟着服务器走：** 每次列出模型、打开模型设置、开始一个任务之前，都会重新问一遍本地服务：你在 LM Studio 里新下载的模型会自己出现，删掉的会自己消失（有角色指名的那个会保留并提示）。服务器连不上就保持原样，不会清空。云端不自动拉——它们的目录动辄上百个，而且列一次要发请求。
- **角色可以跟着服务器走：** 角色的模型写 `auto`（`la init` 对 LM Studio 默认就是这个，界面里是"whatever the server has loaded"），表示"用那台机器上当前加载的模型"，连它实际加载的上下文长度也一并采用。单卡机器上很实用：你在 LM Studio 里换模型，这边不用改任何配置；反过来，写死模型名会让下一个任务把服务器上的模型顶掉。顶栏的下拉框还会标出哪个模型是 `loaded`。**你给任务选了一个本地模型时，子 agent、审查者和提交消息也跟着用它**（它们的角色是 `auto` 的话）——所以任务跑在 GPU 服务器上，帮手不会跑回家里那台去；任务跑在云端时，帮手还是用本地的 `auto`，不额外花钱。注意 `auto` 本身只认得 LM Studio 报告的"已加载"，llama-swap / llama.cpp 这类按需启动的服务它看不出来，所以用 GPU 服务器时请在顶栏直接选模型。
- **发图片和文档：** 输入框可以粘贴截图、拖文件进来，或者点"Attach"。
  - **图片**：点缩略图可以在上面画框、箭头、涂画，圈出哪里不对，再发给模型；图片只发给能看图的模型，当前模型看不了时输入框会提醒你，并给出可以切换的模型。
  - **PDF（比如一篇论文）**：自动抽成文本放在旁边，agent 可以分段 Read、用 Grep 搜；小文档直接放进对话里。
  - 附件放在工作区的 `.la-attachments/`，git 看不到它，也不会被提交；同一个 chat 的后续任务还能引用它。

```bash
.venv/Scripts/la secret set GEMINI_API_KEY                       # 存密钥（输入时不回显）
.venv/Scripts/la init --preset gemini --only gemini-flash-latest # 登记云端模型
.venv/Scripts/la models test gemini-flash-latest --quick         # 只发 1–2 次请求
.venv/Scripts/la run -a paper.pdf "看看这篇论文，给出在本项目里实现的方案" --allow-cloud
```

## M4：桌面界面

```bash
.venv/Scripts/la ui                                   # 打开桌面窗口，进入上次的项目（双击快捷方式也是这个）
.venv/Scripts/la ui -w path/to/project                # 指定先打开哪个项目
.venv/Scripts/la ui --browser                         # 或者在浏览器里打开
```

- **顶栏：**
  - 选择工作文件夹（最近的项目，或者用系统的选文件夹对话框打开别的）
  - **显示当前分支**，以及未提交改动数、待处理的 chat 分支数（每 10 秒刷新，所以你在终端里切换分支也会显示出来）
  - 选择模型、开关思考
- **侧栏：** 这个文件夹下的所有 chat，每个标出它的分支状态（几个提交，或已合并/已关闭）；Files 标签是项目的文件树。
- **对话区：**
  - 流式显示：思考过程可以折叠；每次工具调用是一张卡片，Bash 显示退出码，RunTests 显示 PASSED/FAILED；交接笔记和续跑单独标出
  - 最后给出完成摘要和提交记录
  - 运行中输入框变成"插话"，旁边有停止按钮；危险命令会弹出审批窗口
- **变更面板：** 这个 chat 的提交列表和彩色 diff，以及"合并 / 保留 / 丢弃"三个按钮。
- **安全：** 服务只监听本机，每次启动生成一个随机 token，没有 token 的请求一律拒绝。
- **在线验证：** 用 LM Studio 在界面里完整走了一遍：
  - 修 bug，用了 6 步、52 秒
  - 运行中插话，让它在总结里写明测试数量，最终摘要确实写了
  - 看 diff，合并进 main；main 上测试 6/6 通过
  - 刷新后能从数据库回放整个过程

## M3：每个 chat 一个 git 分支

- 在 git 仓库里执行 `la run` 时，任务在 `<repo>/.la-worktrees/<chat>` 这个 worktree 里进行，分支是 `la/<chat>`，从当前分支分出（可以用 `--base` 指定别的分支）。
- 你的未提交改动会被复制进去，所以 agent 看到的就是你当前的状态。
- 每个任务结束时提交一次，提交消息由模型撰写。同一个 chat 的后续任务接着在这个分支上做。
- 你的检出一直不会被改动：agent 只能写 worktree 里的文件，命令里如果出现你检出目录的路径会被拒绝。
- 相关命令：
  - `la status`：当前分支、未提交改动，以及所有待处理的 chat 分支
  - `la diff [--stat]`：查看这个 chat 做了哪些改动
  - `la merge`：把整个 chat 的改动 squash 成一个提交，合并回 base 分支；你的未提交改动会先暂存，合并后恢复；base 分支没有被检出时，在临时 worktree 里合并，不会切换你的分支
  - `la keep`：保留分支，删掉 worktree 目录；以后续接这个 chat 时会自动重建
  - `la discard`：丢弃这个分支
- 用 `--no-worktree` 可以直接在当前目录里干活。不是 git 仓库的目录会自动这样做。

## M2：跨上下文续跑与会话

- **续跑：** 上下文用到阈值（默认占模型窗口的 65%）时，程序在同一段已缓存的对话里让模型写交接笔记，然后开一个新会话接着做。
  - 新会话只带最新一份笔记。
  - 每个会话至少能增长窗口的 25%。
  - 如果上下文溢出，或模型没写出像样的笔记，就退回到机械生成的操作记录。
- **会话：** `data/localagent.db` 保存 chat、任务和事件。续接一个 chat 时，前面的任务和结果会放进新任务的开头：保留最前 2 条和最新 8 条，每条截到 2000 字符。
- **运行中插话：** 任务运行时在终端输入一行文字，会在下一次调用模型前加入对话。有未处理的插话时，agent 不能结束任务。
- **RunTests 工具：** 自动识别测试命令，第一行直接给出 `[tests PASSED/FAILED · exit code N]`。
- **新增命令：**
  - `la run -c` 继续这个目录最近的一个 chat
  - `la run --chat ID` 继续指定的 chat
  - `la chats` 列出 chat
  - `la show ID` 查看某个 chat 的所有任务

**在线验证：** 把模型窗口人为缩小到 5.5K 来强制交接。
- 修复前：6 个会话用完仍报"未完成"，用了 322 秒、7.2 万 token。
- 修复后：2 个会话完成，用了 90 秒、2.25 万 token。
- 两个串联任务：第二个任务只写了"那句问候"，模型能从 chat 历史里知道指的是哪句。

## M1：核心

- **模型注册表** `data/models.yaml`：以 provider（服务地址）为中心，每个模型挂在一个 provider 下，另外可以给 main / subagent / summarizer 等角色分别指定模型和是否开启思考。
- **OpenAI 兼容流式客户端**：
  - 流式接收推理过程（`reasoning_content`）
  - 拼接分片返回的工具调用
  - 按请求开关思考（LM Studio 用 `reasoning_effort: "none"`）
  - 模型把工具调用写成文本时，也能解析出来
  - 按 provider 限制并发数
- **Agent 循环**：ReAct 加 `finish` 工具，对话只追加、不修改，这样本地服务的前缀缓存能一直命中。
- **工具**：
  - Read / Write / Edit：Edit 前必须先 Read，这条在代码里强制执行
  - Bash：Windows 上用 Git Bash；命令固定在工作目录里执行；危险命令需要确认；自动使用项目自己的 `.venv`
  - Grep / Glob
- **命令行**：`la init`、`la models list|discover|test`、`la run`

**M1 验收（2026-09-11）：** 用 `scripts/demo_fix_bug.py` 让 agent 修复 `examples/buggy_stats` 里的两个 bug。模型是 LM Studio 上的 qwen3.8-27b IQ4_XS，跑在 RTX 2080 Ti 22GB 上。
- 共 6 步、58 秒、1.83 万 token，第一步冷启动 8.3 秒，之后每步首 token 1.3–4.3 秒。
- 事后独立跑 pytest：6/6 通过，测试文件没有被改动。

## 安装（在一台新电脑上）

**Windows：双击 `setup.cmd`。** 它会找到 Python 3.12+、建好 `.venv`、装好依赖，并在**项目文件夹里**生成一个 **LocalAgent** 快捷方式；双击它就直接开窗口（用 `pythonw` 启动，不会弹黑窗）。想放桌面就自己复制一份过去，或者加 `-Desktop` 让它顺便放到桌面和开始菜单。

前提只有一个：装了 **Python 3.12 或更新版本**（[python.org](https://www.python.org/downloads/)，安装时勾上 "Add python.exe to PATH"）。没有 Node，前端构建产物已经在仓库里了。

```powershell
setup.cmd                                        # 双击也行
setup.cmd -BaseUrl http://<你的LM-Studio主机>:1234  # 顺便登记你的模型服务
setup.cmd -Dev                                   # 连测试依赖一起装
setup.cmd -Desktop                               # 快捷方式再放一份到桌面和开始菜单
setup.cmd -NoShortcut                            # 不要快捷方式
```

之后：
- **双击项目里的 `LocalAgent.lnk`**（或 `LocalAgent.cmd`）开窗口。`LocalAgent.cmd` 在没有 `.venv` 时会自己先跑一遍安装，所以把整个文件夹拷到新机器上，双击它一次就能用。快捷方式里存的是本机的绝对路径，所以它被 gitignore 了，每台机器各自生成。
- 命令行：`.venv\Scripts\la run -w <目录> "..."`。
- 换了依赖或者拉了新代码，再跑一次 `setup.cmd` 即可（会复用已有的 `.venv`）。

**macOS / Linux：**

```bash
./scripts/setup.sh                                   # 加 --base-url http://host:1234 / --dev
.venv/bin/la ui                                      # 或 la ui --browser
```

手动安装也可以：

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev,desktop]"   # Windows
# .venv/bin/python -m pip install -e ".[dev,desktop]"     # macOS / Linux
```

## 在远程服务器上的项目里干活（像 VS Code Remote-SSH 那样）

做法和 VS Code 一样：**程序跑在那台服务器上**，你本地只开界面。文件、git worktree、跑测试、venv 全在服务器上发生。

### 直接在界面里登录（推荐）

服务器上装好 LocalAgent 之后（下面第 1 步），本机就不用再开终端了：

1. 点状态栏左下角的机器按钮 → **Add a Machine…**（命令面板里的 "Remote: Manage Machines…" 也行）：ssh host 从你的 `~/.ssh/config` 里选（跳板机、key 全部照旧），填要转发的端口（例如 `18080, 8765`），以及那边打印界面地址的命令（例如 `/srv/you/bin/la-ui`）。
2. 再点左下角的机器按钮，选这台机器。需要密码时会弹一个密码框，显示 ssh 原样的提示（有跳板机时就是跳板一次、目标一次）；第一次连一台新机器会让你核对主机指纹。
3. 连上后窗口直接切到那台机器的 LocalAgent：左下角显示机器名，命令中心里的文件夹名前面带 `[gpu1]`。点左下角的机器按钮选 **Back to This Computer** 回到本机。连接断了会自己重连。

关于密码的安全：它只在内存里从密码框交给本机的 ssh，由 ssh 加密后发出去；不存盘、不写日志、不进数据库；主机密钥校验永远开着，有人冒充服务器时 ssh 直接拒绝、根本不会问密码。点取消会先结束 ssh，不会发出一个空密码。机器列表存在 `data/remotes.yaml`。

关于转发的端口：共享服务器上谁都能在 `127.0.0.1` 上监听。你的服务没在跑的时候（服务器重启后、停掉之后），别的用户可以先占住那个端口，隧道就会把你的请求（代码、对话、API key）送给他，他的回复还能指挥本机的工具。所以连接时先在那边查一遍每个转发端口是谁在监听，之后每 2 秒再查一次：只要有别的用户，就立刻断开、不重连，界面上报出端口和对方的 uid。没人监听不算问题（服务只是没开）。这个检查读的是 Linux 的 `/proc/net/tcp`；那边不是 Linux 时照样连接，状态里注明"没法检查"。

### 手动做法

1. 在服务器上装一次（不需要桌面环境）：

```bash
./scripts/setup.sh --headless
```

2. 从你自己的机器连过去，同时把界面端口转发回来（如果服务器连不上你的模型服务，顺便用 `-R` 把模型也转发过去）：

```bash
ssh -L 8765:localhost:8765 -R 1234:<你的LM-Studio地址>:1234 <你>@<服务器>
```

3. 在这个 ssh 会话里（建议放在 tmux/screen 里，断线不丢）：

```bash
.venv/bin/la ui --browser --no-open --port 8765
```

它会打印一行 `http://127.0.0.1:8765/#token=…`，把它粘到**你自己机器的浏览器**里就能用了。界面里的"选文件夹"列的是**服务器上的目录**。

几点说明：

- **要跳板机（ProxyJump）也一样：** `-L` / `-R` 作用在**最终那台机器**上，跳转由 ssh 客户端自己处理。在 `~/.ssh/config` 里给它写一个专用别名最省事：

```
Host mylab-la
    HostName ada1.example.edu
    User you
    ProxyJump gateway
    LocalForward 8765 localhost:8765
    RemoteForward 1234 192.168.1.20:1234
```

  之后 `ssh mylab-la` 一条命令就把跳板、两个方向的转发全办了。（`RemoteForward` 的目标地址是**你本机**解析的，所以它指向的是你这边能访问到的模型服务。）
- **服务器怎么访问模型：** 上面 `-R` 那段把你的 LM Studio 通过这条 ssh 连接转发到服务器的 `127.0.0.1:1234`，服务器上 `la init --base-url http://127.0.0.1:1234` 即可，不需要在服务器上装 Tailscale，也不需要开任何入站端口。（如果服务器的 sshd 禁了 `AllowTcpForwarding`，就只能在服务器上直接装 Tailscale，或者给它配一个云端模型。）
- **安全：** 服务默认只监听 `127.0.0.1`，只能通过这条 ssh 隧道访问；每次启动随机生成 token，没有 token 的请求一律 401；token 在 URL 的 `#` 片段里，不会发给服务器也不会进日志。
- **服务器拉不到私有仓库时**（没有 GitHub key，或者 GitHub 太慢）：从你本机用 ssh 推过去。服务器上 `git init -b main <目录> && git -C <目录> config receive.denyCurrentBranch updateInstead`，本机 `git remote add <名字> <服务器>:<目录> && git push <名字> main`。以后要更新，本机 push 一下即可。
- **家目录用不了时**（配额满，或者网络盘暂时读不到）：`LOCALAGENT_HOME` 放到本地盘，`GIT_CONFIG_GLOBAL` 指向本地盘上的一份 `.gitconfig` 副本（不然 agent 提交时可能拿不到你的 git 身份），安装时再加上 `UV_CACHE_DIR`、`PIP_CACHE_DIR`。可以写一个两三行的 `la` 包装脚本，把这些变量设好再 `exec .venv/bin/la "$@"`。
- **服务器上要有：** Python 3.12+（很多集群要先 `module load`；没有的话 `uv python install 3.12`，不需要 root）和 git。数据库、models.yaml、skills 都在服务器的 `data/` 下，和你本机是两套。
- **注意跑测试的地方：** agent 在哪台机器上就在哪台机器上跑测试。集群上别在登录节点跑重的测试套件，先 `srun --pty bash` 要一个交互节点再启动。
- **不要**把远程目录挂载成盘（sshfs/SMB）再指过去：文件能读写，但命令还是在你本地跑，测试用的是你本地的 Python，worktree 创建还要走网络复制。

## 把模型放到有 GPU 的服务器上（有请求才启动，闲置自动退出）

适合有空闲大显存 GPU 的共享服务器：模型只在有请求时占一张卡，15 分钟没有请求就退出，把显存还回去。`scripts/gpu-server/` 是一套实际在用的配置（A100 80GB，Qwen3.8-27B Q8_0，262K 上下文，带视觉和 MTP），实测数字见 [design.md §1.6](docs/design.md)。

```
笔记本上的 LocalAgent ──ssh -L 18080──▶ 服务器 127.0.0.1:18080  llama-swap（常驻，不占 GPU）
                                             │ 来了第一个请求：start-llm.sh 挑一张空卡，启动 llama-server（约 14 秒）
                                             │ 15 分钟没有请求：停掉 llama-server，显存归还
```

1. **在服务器的本地盘上建一个工作目录**，uv、Python、缓存、模型、编译产物全放在里面，不碰家目录（集群的 NFS 家目录配额经常是满的）：

```bash
export W=/local/disk/you/localllm
mkdir -p $W/bin $W/models $W/logs $W/secrets
export XDG_CACHE_HOME=$W/.cache XDG_CONFIG_HOME=$W/.config UV_PYTHON_INSTALL_DIR=$W/.local/python HF_HOME=$W/.cache/huggingface
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=$W/bin UV_NO_MODIFY_PATH=1 sh
$W/bin/uv venv --python 3.12 $W/.venv && $W/bin/uv pip install --python $W/.venv/bin/python "huggingface_hub[hf_xet]"
$W/.venv/bin/hf download <GGUF 仓库> <模型>-Q8_0.gguf mmproj-<模型>-F16.gguf --local-dir $W/models/dl
# 换成固定的短名字：start-llm.sh 按这个名字找文件；同一台服务器上的其他用户用 ps 能看到 llama-server 的完整命令行（模型路径、别名）
mkdir -p $W/models/qwen3.8-27b && mv $W/models/dl/<模型>-Q8_0.gguf $W/models/qwen3.8-27b/qwen3.8-27b-q8_0.gguf \
  && mv $W/models/dl/mmproj-<模型>-F16.gguf $W/models/qwen3.8-27b/mmproj-qwen3.8-27b-f16.gguf && rm -rf $W/models/dl
```

`<GGUF 仓库>` 选一个 Qwen3.8 27B 的 GGUF 发布：要带 MTP 头（`start-llm.sh` 用它做投机解码）和配套的 mmproj 视觉投影（看图用）。换别的模型时，把 `start-llm.sh` 里的文件名和参数一起改掉。

2. **编译 llama.cpp**（官方没有 Linux 的 CUDA 预编译包）。CUDA 版本不能高于驱动支持的版本（`nvidia-smi` 右上角），A100 的算力架构是 80。服务器上 `git clone` GitHub 可能很慢，直接下发布版的源码包更快：

```bash
curl -L https://github.com/ggml-org/llama.cpp/archive/refs/tags/b10931.tar.gz | tar -xz -C $W && mv $W/llama.cpp-b10931 $W/llama.cpp
cd $W/llama.cpp && cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_COMPILER=/usr/local/cuda-12.6/bin/nvcc -DCMAKE_CUDA_ARCHITECTURES=80 -DCMAKE_BUILD_TYPE=Release && cmake --build build -j --target llama-server
```

3. **装 llama-swap、生成 API key、启动**。先在你本机把三个脚本拷过去：`scp scripts/gpu-server/* <服务器>:<W>/`。然后在服务器上：

```bash
curl -L https://github.com/mostlygeek/llama-swap/releases/download/v255/llama-swap_255_linux_amd64.tar.gz | tar -xz -C $W/bin llama-swap
(umask 077; printf "sk-%s" "$(head -c 32 /dev/urandom | base64 | tr -d '/+=')" > $W/secrets/api.key)
chmod +x $W/*.sh && $W/run-swap.sh
```

4. **在你本机开隧道、存 key**（隧道要一直开着，可以放进 ssh config 的 `LocalForward 18080 127.0.0.1:18080`）：

```bash
ssh -N -L 18080:127.0.0.1:18080 <服务器>
```

```bash
ssh <服务器> cat <W>/secrets/api.key | la secret set GPU_SERVER_API_KEY
```

然后在 `data/models.yaml` 里加上（`la init` 探测不到下面这几项，要手写）：

```yaml
providers:
  gpu-server:
    base_url: http://127.0.0.1:18080/v1
    api_key_env: GPU_SERVER_API_KEY
    max_concurrency: 4        # start-llm.sh 开了 4 个槽位
models:
  gpu-qwen3.8-27b:
    provider: gpu-server
    model_id: qwen3.8-27b     # llama-swap.yaml 里的模型名
    context_length: 262144
    max_subtask_context: 131072   # 524K 的 KV 池放得下主 agent 加一个大的子 agent；本地小卡别设
    max_parallel_subtasks: 3      # 同一轮的只读子 agent 同时跑；比服务器槽位数少一个
    vision: true
    thinking_off:
      chat_template_kwargs: {enable_thinking: false}
roles:
  main: {model: gpu-qwen3.8-27b, thinking: true}   # 其余角色留 auto 或不写：它们跟着任务用的模型走
```

最后用 `la models test gpu-qwen3.8-27b` 验一遍。几点说明：

- **`main` 不能写 `auto`：** 只有 LM Studio 会报告"当前加载了哪个模型"，这里要写模型名。子 agent、审查者、提交消息这几个角色留 `auto`（或者干脆不写）就行，它们会跟着任务实际用的模型走——你在顶栏临时换成家里的模型，它们也跟着换过去。
- **`context_length`、`vision` 必须手写：** 通用的 `/v1/models` 不带这些信息，不写就是 32K、看不了图（图片会被换成一行文字说明）。
- **关思考**用 `chat_template_kwargs`，不是 LM Studio 那个 `reasoning_effort`。
- **第一个请求要等模型启动**（约 14 秒）。空闲退出后前缀缓存也没了，长对话恢复后的第一条要重新 prefill：10 万 token 约 100 秒。
- **只用空卡：** 有进程或占用超过 1 GiB 的卡都跳过，按 `GPU_ORDER` 的顺序挑；四张都被占就报错，不和别人抢。
- **为什么要 API key：** 共享服务器上 `127.0.0.1` 对所有登录的用户都可见。llama-swap 和 llama-server 都要求带 key。
- **端口被别人占了就不启动：** 模型卸载后 llama-server 的端口（18100）是空的，别的用户可以先占住。那样 llama-server 绑不上，llama-swap 的健康检查却会连到对方的程序，然后把请求转给他。所以 `start-llm.sh` 启动前先确认端口没人占，启动后再确认第一个监听的是自己；不是就停下并写进 `logs/start-llm.log`。
- **停掉：** `tmux kill-session -t la-swap`。就算 llama-swap 被杀，`start-llm.sh` 里的守护进程也会在几秒内停掉 llama-server，不会留下一个占着显存、再也没人管的进程。日志在 `$W/logs/`。

## 使用

```bash
# 1. 登记本地模型服务（会自动识别 LM Studio，并读取上下文长度等信息）
.venv/Scripts/la init --base-url http://<lmstudio-host>:1234

# 2. 查看注册表，并测试模型：工具调用、prefill 速度、前缀缓存、生成速度、思考开关
.venv/Scripts/la models list
.venv/Scripts/la models test

# 3. 在某个项目目录里执行任务
.venv/Scripts/la run -w path/to/project "测试挂了，找出 bug 并修好，不要改测试"
```

`la run` 的常用参数：
- `-c` / `--chat ID`：接着上一个（或指定的）chat 做
- `-a FILE`：带上图片或文档（可以重复）
- `--review`：做完之后让审查者读一遍 diff，有问题就修（和原改动一起提交）
- `--no-subtasks`：不给 Task 工具，不用子 agent
- `--scratch`：不在项目里干活，用 `data/scratch`
- `--allow-cloud`：允许 agent 自己切到云端模型
- `--no-thinking`：本次运行关闭思考
- `--show-thinking`：实时显示推理过程
- `-y`：危险命令（`git push`、`rm -rf` 等）不再逐条确认
- `-m MODEL`：指定模型
- `--base BRANCH` / `--no-worktree`：从哪个分支分出来 / 不建分支，直接在当前目录改

完整列表：`la run --help`。

## 开发

```bash
.venv/Scripts/python -m pytest            # 离线测试（294 个），用脚本化的本地 HTTP 服务模拟模型
.venv/Scripts/python -m pytest -m live    # 连真实模型的端到端测试（需要先 la init）
```

## 路线图

M1–M7 都已完成，见 [docs/design.md](docs/design.md) 第 6 节。还没做的：

- **本地加云端真正并行**（`run_parallel`）：单卡上并行不划算（实测两路并发总吞吐略降），所以留给"本地模型 + 云端模型"这种组合。
- **"建议的下一步"**：任务结束后给出几个可以点的后续动作。
- **接着某个历史任务继续**，以及命令和任务的自动补全。
- **云端花费上限**：现在只显示花了多少，还不能设上限。
- **MCP**：能做，但要小心——每接一个 server 就把它的工具定义加进提示词前缀（我们一直守在 2,300 token），而且小模型在 30+ 工具时挑错工具的概率明显上升。真要做就按项目白名单，只暴露挑出来的几个工具。

明确决定**不做**的：让 agent 给每个项目自己写记忆（KISS Sorcar 实测这样反而降低质量——记下来的"项目约定""文件位置"很快过期，而小模型比前沿模型更容易照着过期的记录走；需要长期生效的规则写进项目自己的 `CLAUDE.md` / `AGENTS.md`，agent 每个任务都会读）。

## 许可证

[Apache License 2.0](LICENSE)。随界面一起分发的第三方组件（Monaco、Codicons 等）的许可和署名见 [NOTICE](NOTICE)。
