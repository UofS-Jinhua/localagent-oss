# LocalAgent

[English](README.md) | **中文**

一个类似 Claude Code / Codex 的编码代理，**为本地大模型设计**：LM Studio、Ollama、llama.cpp、vLLM，或者任何提供 OpenAI 兼容接口的服务；也可以接云端模型。它能读改代码、跑命令和测试，每个 chat 都在自己的 git 分支上干活。分层架构参考 [KISS Sorcar](https://github.com/ksenxx/kiss_ai)。

本地模型的取舍和云端不一样：prefill 慢，长对话全靠服务器的前缀缓存撑着，上下文窗口小，思考模型还容易在思考里绕圈子。LocalAgent 就是围绕这些来设计的：

- **对话只追加、不修改**，系统提示词也永远不变。所以每一步都能复用服务器的缓存，不用把整段对话重算一遍。
- 系统提示词加工具定义控制在约 2,300 token。
- 上下文快满时，模型先写一份交接笔记，再开一个新会话接着做。
- 思考有自己的上限，和输出上限分开。一步没调工具时，先减少思考重试这一步，重试几次都不行才判定任务失败。
- 子 agent 用来把大段的工具输出挡在主对话之外。它省的是上下文，不是时间。

这些取舍背后的实测数据见 [docs/design.md](docs/design.md)。

## 功能

- **桌面窗口，或者在浏览器里用。** 界面仿 VS Code：资源管理器、源代码管理、用 Monaco 看文件和左右对比的 diff、命令面板（`Ctrl+P`、`Ctrl+Shift+P`）。另有命令行 `la`。
- **工具：** Read、Write、Edit（改文件前必须先读过）、Bash（Windows 上用 Git Bash）、Grep、Glob、RunTests、Plan、AskUser、WebSearch 和 WebFetch、Task（子 agent）、Skill、set_model。
- **每个 chat 一个 git 分支。** agent 在 `la/<chat>` 分支的 worktree 里干活，从不碰你的检出。做完可以在界面或命令行里合并（压成一个提交）、保留或丢弃。不是 git 仓库的文件夹也能用。
- **长任务。** 交接笔记让一个任务跨多个上下文窗口接着做；同一个 chat 里的下一个任务接着上一个的进度。运行中可以随时插话，也可以停下。
- **审查者。** 可以打开一个只读的审查者：提交前读这次的 diff、自己跑测试。它可以是另一个模型，甚至另一家厂商的。发现的问题交回主模型去修，修完和原改动一起提交。
- **模型。** 本地服务，外加云端预设：Gemini、OpenAI、Anthropic（它的 OpenAI 兼容接口）、DeepSeek、OpenRouter、通义千问（DashScope）。API key 只存在 `data/secrets.env`，不进模型注册表，也不进 agent 执行命令时的环境变量。角色写 `auto` 时，用 LM Studio 当前加载的那个模型。花费按步计算。云端模型只有你允许时才会用。
- **附件。** 截图可以粘贴或拖进来，还能在上面画框、画箭头；PDF 转成文字，能看图的模型还可以把某一页渲染成图片来看。
- **Skill。** 一个文件夹放一个 `SKILL.md`，格式和 Claude Code 相同。提示词里只放名字和一行说明，模型需要时再打开正文。
- **远程机器。** 像 VS Code Remote-SSH 那样：agent 跑在服务器上，窗口留在你的电脑上。直接在 app 里登录，跳板机和密码提示都支持。在共享服务器上，转发的端口会被检查，别的用户截不走。你的 skill 会自动拷到服务器上。
- **GPU 服务器脚本。** llama.cpp 挂在 llama-swap 后面：有请求才启动，空闲 15 分钟自动退出；另有一个自用的 SearXNG 给联网搜索用。

## 环境要求

- Python 3.12 或更新的版本（[python.org](https://www.python.org/downloads/)，Windows 上安装时勾上 "Add python.exe to PATH"），以及 git。
- Windows 10 / 11（窗口用的是 WebView2）、macOS 或 Linux。Windows 上要装 Git for Windows：Bash 工具跑在 Git Bash 里。
- 一个模型：LM Studio 这类 OpenAI 兼容的服务，或者某个云端模型的 API key。
- **不需要** Node.js：构建好的界面已经在仓库里了。

## 安装

**Windows：** 双击 `setup.cmd`。它会找到 Python 3.12+、建好 `.venv`、装好依赖，并在项目文件夹里生成一个 **LocalAgent** 快捷方式。快捷方式打开窗口时不会弹黑窗。

```powershell
setup.cmd                                        # 双击也行
setup.cmd -BaseUrl http://<LM-Studio主机>:1234    # 顺便登记你的模型服务
setup.cmd -Dev                                   # 连测试依赖一起装
setup.cmd -Desktop                               # 快捷方式再放一份到桌面和开始菜单
setup.cmd -NoShortcut                            # 不要快捷方式
```

之后打开 `LocalAgent.lnk` 或 `LocalAgent.cmd` 就行（没有 `.venv` 时，`LocalAgent.cmd` 会先自己跑一遍安装）。拉了新代码后再跑一次 `setup.cmd`。

**macOS / Linux：**

```bash
./scripts/setup.sh      # 选项：--base-url http://host:1234、--dev、--headless（没有桌面的服务器）
.venv/bin/la ui
```

**手动安装：**

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev,desktop]"   # Windows：.venv\Scripts\python
```

## 快速上手

下面的 `la`，在 Windows 上是 `.venv\Scripts\la`，其他系统是 `.venv/bin/la`（激活了 venv 就直接用 `la`）。

```bash
# 1. 登记你的模型服务。会自动识别 LM Studio，并读取各模型的上下文长度。
la init --base-url http://<主机>:1234

# 2. 列出模型并测一个：工具调用、prefill 速度、前缀缓存、生成速度、思考开关。
la models list
la models test

# 3. 用 app 打开一个项目（加 --browser 就在浏览器里打开，不开窗口）……
la ui -w path/to/project

# ……或者直接在终端里跑一个任务。
la run -w path/to/project "测试挂了，找出 bug 并修好，不要改测试"
```

`la run` 常用的选项：

| 选项 | 作用 |
|---|---|
| `-c`、`--chat ID` | 接着这个文件夹最近的一个 chat，或者指定的 chat |
| `-a FILE` | 带上图片或文档（可以重复） |
| `--review` | 做完后让审查者读 diff，有问题就修 |
| `--scratch` | 不在项目里干活，用一个 scratch 文件夹 |
| `--allow-cloud` | 允许 agent 自己切到你标为可切换的云端模型 |
| `--no-thinking`、`--show-thinking` | 关掉思考，或者实时显示思考 |
| `--no-subtasks` | 不用子 agent |
| `-y` | 危险命令（`git push`、`rm -rf` 等）不再逐条确认 |
| `-m MODEL`、`--base BRANCH`、`--no-worktree` | 指定模型、从哪个分支开始，或者不建 chat 分支直接改 |

命令行管理 chat 分支：`la status`、`la diff`、`la merge`、`la keep`、`la discard`。看以前的 chat：`la chats`、`la show ID`。另外还有 `la secret`、`la web`、`la skills`、`la remote`；每个命令都可以加 `--help` 查看说明。

**云端模型：**

```bash
la secret set GEMINI_API_KEY                         # 输入 key 时不回显
la init --preset gemini --only gemini-flash-latest
la models test gemini-flash-latest --quick           # 只发一两次请求
```

也可以在 app 里加：左下角的齿轮 → Models。

## 数据放在哪

app 写的所有东西都在检出目录的 `data/` 下：模型注册表（`models.yaml`）、API key（`secrets.env`）、chat 数据库、附件、scratch 文件夹，以及远程机器、联网搜索和 skill 的设置。`data/` 已经被 git 忽略。想放到别处就设 `LOCALAGENT_HOME`；装成 wheel 时默认是 `~/.localagent`。

## 服务器和 GPU

[docs/servers.md](docs/servers.md) 讲了这些：

- 在 app 里直接操作远程服务器上的项目，或者手动用 `ssh -L` 连过去；
- 服务器上没有 GitHub 凭据时，怎么把代码推过去；
- 把模型放到共享的 GPU 服务器上，有人用时才启动（llama.cpp 加 llama-swap，脚本在 [scripts/gpu-server/](scripts/gpu-server/)）；
- 跑一个 SearXNG 给联网搜索用，以及用一条命令管理这几个服务。

## 文档

- [docs/architecture.md](docs/architecture.md)：分层、模块地图、数据流、不变量和事件。改代码前先看它。
- [docs/design.md](docs/design.md)：设计方案、背后的实测数据、里程碑。
- [docs/changelog.md](docs/changelog.md)：每次改了什么、为什么改、实测的数字，最新的在最上面。
- [docs/kiss-sorcar-notes.md](docs/kiss-sorcar-notes.md)：参考设计 KISS Sorcar 的笔记。

## 开发

```bash
.venv/bin/python -m pytest            # 离线测试：用一个脚本化的本地 HTTP 服务代替模型
.venv/bin/python -m pytest -m live    # 端到端测试，连你注册表里的真实模型
```

测试在 Windows 和 Linux 上都能通过。界面代码在 [frontend/](frontend/)（React、TypeScript、Vite）。改了 `frontend/src` 之后，在 `frontend/` 里跑 `npm install` 和 `npm run build`，把重新构建的 `src/localagent/server/static` 一起提交。写给编码 agent 的注意事项（对人也适用）见 [CLAUDE.md](CLAUDE.md)。

## 路线图

- 本地模型和云端模型在同一个任务里并行干活。
- 任务结束时给出几个建议的下一步。
- 接着任意一个历史任务继续，以及命令和任务的自动补全。
- 云端模型的花费上限。
- MCP：按项目白名单接入。每接一个 server，它的工具定义都要占提示词；而且工具超过 30 个时，小模型挑错工具的概率明显上升。

明确不做的：让 agent 给每个项目自己写记忆。KISS Sorcar 实测这样反而降低质量：记下来的东西很快过期，而小模型比前沿模型更容易照着过期的记录走。需要长期生效的规则，写进项目自己的 `CLAUDE.md` 或 `AGENTS.md`，agent 每个任务都会读。

## 许可证

[Apache License 2.0](LICENSE)。随界面一起分发的第三方组件（Monaco、Codicons、React 等）列在 [NOTICE](NOTICE) 里，许可原文见 [THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt)。
