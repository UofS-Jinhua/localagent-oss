# 在服务器上用 LocalAgent

程序跑在服务器上、本机只开界面（像 VS Code Remote-SSH 那样），以及把模型放到有 GPU 的服务器上、按需启动。下面的 `gpu1` 是 `~/.ssh/config` 里那台服务器的别名，`<base>`、`/srv/you` 是你在那边放东西的目录。

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

## 把代码更新到服务器

服务器上没有你的 GitHub 凭据（也不该放）时，那边 `git pull` 会卡在要用户名。反过来推就行 —— 走的是已经能用的 ssh：

```bash
git push gpu1 main
```

一次性准备：本机 `git remote add gpu1 ssh://gpu1/<base>/LocalAgent`，服务器上 `git -C <base>/LocalAgent config receive.denyCurrentBranch updateInstead`（这一条让推送直接更新那边的工作区，前提是它干净）。

推完还要做的：

- **改了 `data/` 里的东西不会跟着走**（`data/` 不进 git）。服务器有自己的注册表（那边的 `data/models.yaml`，或者 `LOCALAGENT_HOME` 指向的目录），模型设置要在那边单独改一遍。
- **界面不会自己用上新代码**：`ssh gpu1 <base>/bin/la-services restart ui`。重启后 token 会变，app 里点一次 Connect 就好。
- 顺手在那边跑一遍测试，Linux 上的行为和 Windows 不一样的地方（进程组、路径）就是靠它兜底：`ssh gpu1 'cd <base>/LocalAgent && .venv/bin/python -m pytest -q'`。

## 把模型放到有 GPU 的服务器上（有请求才启动，闲置自动退出）

适合有空闲大显存 GPU 的共享服务器：模型只在有请求时占一张卡，15 分钟没有请求就退出，把显存还回去。`scripts/gpu-server/` 是一套实际在用的配置（A100 80GB，Qwen3.8-27B Q8_0，262K 上下文，带视觉和 MTP），实测数字见 [design.md §1.6](design.md)。

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

## 联网搜索：在服务器上跑 SearXNG

共享服务器、云主机的出口 IP 常被搜索引擎当成机器人（2026-09-15 实测，从一台服务器访问 DuckDuckGo 简版页被拦，返回 202）。SearXNG 是一个开源的"元搜索"，替你去问 Google、Brave 等，只给你一个人用时不容易被拦。不需要 root 和 docker：

```bash
ssh gpu1 mkdir -p <base>/searxng && scp scripts/gpu-server/*searxng.sh gpu1:<base>/searxng/
```

然后在服务器上：

```bash
cd <base>/searxng && ./setup-searxng.sh && ./run-searxng.sh
```

```bash
la web set --search searxng --url http://127.0.0.1:18888
```

- `setup-searxng.sh` 下载源码包（服务器上 git clone GitHub 很慢）、用 uv 建虚拟环境、生成只监听 127.0.0.1:18888、打开 json 格式的 `settings.yml`。再运行一次就是更新（`settings.yml` 保留）。需要 `uv`，不在 PATH 上时用 `UV=/path/to/uv ./setup-searxng.sh`。
- `run-searxng.sh` 在 tmux 会话 `la-search` 里启动；平时用下面「服务器上的三个服务」里的 `la-services` 一起管，不用单独记。
- 实测：一次查询约 0.7 秒（中文约 3 秒），结果来自 Google CSE 和 Brave；DuckDuckGo 出验证码，SearXNG 会自动跳过它。

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
