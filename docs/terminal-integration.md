# 终端集成：在自己的 shell 里 `@x` 与 `@c`

不进 REPL。你照常在 shell 里敲命令，卡住了就 `@x 问题`——小羽带着**你刚跑过的那些命令和它们的退出码**回答，并且续写同一个会话，可以接着追问。只是想不起某条命令怎么写，用 `@c 一句话需求`：它只给一条命令，放回你的提示符，由你回车（见下文「`@c`：一句话换一条命令」）。

```text
$ make test
…一屏报错…
$ @x 这个报错是什么意思，先从哪查
会话 term-3f9a1c2e · 带上 1 条命令
  make test 报的是 …
$ @x 那把失败的那个用例单独跑一下
会话 term-3f9a1c2e · 带上 1 条命令 · 接上 2 条消息
```

## 安装

```bash
xiaoyu term install              # 按 $SHELL 认出 zsh / bash / fish，把那一行写进启动文件
xiaoyu term install --natural    # 选项与 term init 相同（--name / --command-not-found / --natural），重跑即改
xiaoyu term uninstall            # 移除；xiaoyu uninstall 也会顺手收走
```

它先打出要写哪个文件、写哪一行，你确认后才动手（`--yes` 跳过确认，`--dry-run` 只看不写），写前留一份 `.bak`。写进去的是首尾带标记的一小段，重跑只替换这一段、不会重复加；文件里已经有你自己手写的 `term init` 那一行就原样不动。bash 在 macOS 上写 `~/.bash_profile`（系统终端开的是登录 shell，不读 `.bashrc`），zsh 认 `$ZDOTDIR`。写完新开一个终端生效，或在当前终端 `source` 一下。`xiaoyu doctor` 会报告接没接上。

想自己动手，或者用的是 PowerShell（`$PROFILE` 的位置随版本而变，install 不猜），把下面这一行放进启动文件：

| shell | 放进启动文件的那一行 |
|---|---|
| zsh | `~/.zshrc`：`eval "$(xiaoyu term init zsh)"` |
| bash | `~/.bashrc`：`eval "$(xiaoyu term init bash)"` |
| fish | `~/.config/fish/config.fish`：`xiaoyu term init fish \| source` |
| PowerShell | `$PROFILE`：`Invoke-Expression (xiaoyu term init powershell \| Out-String)` |

脚本做三件事：

1. 导出 `XIAOYU_TERM_SESSION`（这个终端的会话 id）和 `XIAOYU_TERM_PENDING`（记命令的文件）；
2. 定义 `@x` / `@xiaoyu`（zsh 里是 `noglob` 别名，问题里的 `?` `*` `[` 不会被当通配符；PowerShell 里 `@` 是 splatting 语法当不了命令名，改叫 `x` / `Ask-Xiaoyu`），zsh 与 bash 里另有 `@c`；
3. 挂两个钩子：命令开跑前记下命令行（zsh `preexec`、bash `DEBUG` trap、fish `fish_preexec`），跑完后补记退出码（zsh `precmd`、bash `PROMPT_COMMAND`、fish `fish_postexec`）；PowerShell 包 `prompt`，在下一个提示符出现前把 `Get-History` 的增量和退出码一起记下。钩子只用 shell 内建追加一行文本，**不起 Python 进程**，shell 不会因此变卡。

重复 eval 不会挂两次钩子。`xiaoyu` 不在 PATH 上（venv 没激活）时，脚本会钉死生成它的那个解释器 `-m xiaoyu`；要指定别的写法用 `--launcher`。

## 用法

```bash
@x 刚才那个报错怎么回事                 # 带着自上次提问以来跑过的命令
@x                                       # 不带问题：回车后在「问 ›」提示符下输入，原样读入
cat err.log | @x 这是什么错              # 管道内容当材料，和 xy -p 一样
@x --model gpt-6 换个模型看看这个问题     # --model / --mode / --effort / --yolo 同主命令
xiaoyu term info                         # 一行：会话 id · 模型 · 已用 token · 待交付命令数
```

- 一次提问带的是**自上次提问以来**的命令，交付过的不再重复带；两次提问之间没敲过命令就不带前缀。
- **问题里有特殊字符时只敲 `@x` 回车**，再在「问 ›」提示符下输入。写在命令行上的问题要先过 shell 的解析：引号要配对，括号、`|`、`>`、`&`、`;` 有语法含义，`$变量` 会被展开——`@x 这个 (x) 是什么` 在哪种 shell 里都问不出去。提示符下读到的是原样文本，什么都不用转义。`--model` 这类旗标照常写在 `@x` 后面。zsh 下 `?` `*` `[` 不必走这条路，`@x` 已经是 `noglob` 别名。
- 审批照常：stdin 是终端就逐条确认，不默认 `--yolo`。管道喂进来的提问（stdin 不是终端）按无人值守处理——拒绝需要确认的工具。
- 工作目录就是当前 shell 的目录；每个终端一段对话，`cd` 到哪都接着聊。
- `xiaoyu term info` 放进提示符能看到会话与待交付数（走快路径，不导入 agent，百毫秒内）。

命令是这样交给模型的（放在你的问题前面，整段裹在 `<untrusted_content>` 里——命令行可能是粘贴来的，里面的"指令"不算数）：

```text
[终端上下文] 自上次提问以来，你在这些目录跑过这些命令（按时间；只有命令文本与行尾的退出码、没有输出，需要结果可以自己重跑）：
<untrusted_content>
# ~/proj
10:21  $ make test  → 退出码 2
10:23  $ git diff --stat  → 退出码 0
</untrusted_content>

这个报错是什么意思
```

行尾没有退出码的命令是没记到：还没跑完、终端被关掉、用 `term log` 手动记的，或者这个终端里加载的还是旧版脚本（升级后开个新终端即可）。

## `@c`：一句话换一条命令

0.63.0 起。只在 **zsh 与 bash** 里有。

```text
~/proj % @c 找出当前目录下大于 100M 的文件，按大小倒序
列出大于 100M 的文件并按大小倒序
~/proj % find . -type f -size +100M -exec du -h {} + | sort -rh█
```

第二个提示符上的命令是放上去的，没有执行：可以改，回车才跑，Ctrl-C 就放弃。

- **zsh**：命令直接出现在下一个提示符上。
- **bash**：函数里写不了行编辑器的缓冲区，命令打出来并推进历史——**按一次 ↑ 就是它**（`@c …` 这一行自己不留在历史里）。
- **可以追问**：`@c 只看 .log，排除 node_modules` 会在上一条的基础上改。记得住这个终端最近 4 次、30 分钟之内给过的命令。
- **回路是通的**：你回车执行的那条命令照常被钩子记下，之后 `@c 修一下` 或 `@x 为什么失败` 都带得上它和它的退出码（没有输出）。
- **不是一条命令能办的事**（要讲解、要总结、要好几步、信息不够）它不会硬给，而是把**同一句需求直接转给 `@x`**（屏幕上会打一行「转给 @x」），不用再敲一遍；管道材料一并带过去。之后就是一次普通的 `@x`：续写本终端的会话、带上待交付的命令、逐条审批。提示符上什么都不放。开了 `--natural` 时也一样——敲一句中文，能变成一条命令就上提示符，变不成就由 `@x` 接手。
- 需求里有引号、括号、`|`、`$` 时只敲 `@c` 回车，再在「要什么命令 ›」提示符下输入，原样读入。`cat access.log | @c 提取访问最多的 10 个 IP` 这样把管道内容当材料也行。
- 旗标只认写在需求**前面**的：`@c --model gpt-6 …`、`@c --effort high …`。需求里出现的 `-rf`、`--verbose` 不会被当旗标。

和 `@x` 的区别，也是它存在的理由：

| | `@x` | `@c` |
|---|---|---|
| 做什么 | 一整轮 agent：会读文件、会自己跑命令 | 只把一句话翻成一条命令 |
| 命令在哪跑 | 小羽的子进程里，逐条审批 | **你自己的 shell 里**，你回车 |
| 适合 | 排查、修改、要它动手的事 | `cd` / `export` / 激活 venv / `ssh` / `sudo` / 交互程序，以及想留在历史里的命令 |
| 会话 | 续写本终端的会话 | 不进会话，不取走待交付的命令（转给 `@x` 的那一次除外） |
| 耗时 | 随任务 | 一两秒 |

**它不过审批。** 命令是你自己回车执行的，小羽的权限判定、沙箱都不参与——放上提示符的命令请照常看一眼。能认出来的两种形态会多一行提示：强制删除（`rm -f` / `-rf`）与提权（`sudo` 等）；认不出来不代表安全。

**你的别名会顶掉同名命令。** 小羽探环境只看 PATH，不知道你的 shell 里 `ipconfig` 可能是一个跑 `ifconfig | awk …` 的别名；给出的 `ipconfig getifaddr en0` 回车后走的是别名，结果不对。所以命令放上提示符之前，脚本在**你的 shell 里**查一遍命令位置上的词（行首、`|` `&&` `||` `;` 之后），撞上别名就多打一行：

```text
注意：ipconfig 在你的 shell 里是别名（→ ifconfig | awk …），回车会按别名跑；要原生命令请改成 command ipconfig
```

只给同名命令加参数的别名（`ls='ls -G'`、`grep='grep --color=auto'`）不提示；函数不查（`cd` 这类常被函数包一层，查了全是噪音）。别名只在本机查，不发给模型。命令进提示符之前会摘掉终端控制序列，双向文本控制字符换成可见的 `\uXXXX`。

**用哪个模型**：默认用辅助模型（`XIAOYU_SUMMARY_MODEL`，与对话压缩同一个），它没有 provider 能接时用主模型；`--model` 点名。推理深度默认 `low`——把一句话翻成一条命令不需要多想，想得深只是让你多等。单次请求最多等 30 秒。

### 本机环境只探一次

BSD 与 GNU 的 `sed -i`、`date -d`、`stat` 写法不同，是命令给错的头号原因。第一次 `@c` 时小羽探一遍本机并记下，之后直接读：

```text
已记下本机环境：macOS 27.0 · arm64 · zsh 5.9 · BSD 工具链 · 包管理 brew
```

记的是：系统与版本、架构、shell 与版本、基础命令是 BSD / GNU / BusyBox 哪一套、有哪些包管理器、一份常用工具清单里哪些装了哪些没装（`rg` `fd` `jq` `gsed` `docker` `pbcopy` `systemctl` …）。全是读文件和查 PATH，不起子进程、不出网。

文件在 `<配置目录>/term/environment-<shell>.json`，每种 shell 一份。**系统、架构或 shell 版本变了会自动重探**，另外每 7 天重探一次（跟上工具的装卸）。刚装了新工具想立刻生效：删掉这个文件。

有三件事不进这份记录，**每次 `@c` 现读**，因为同一台机器上它们也会变：

| 现读的 | 影响什么 |
|---|---|
| 是不是 root，不是的话有没有 `sudo` | 已经是 root 不加 `sudo`；普通用户才加；没有 `sudo` 时给 root 下能直接跑的写法，并说明要换 root 来跑 |
| 是不是 SSH 进来的会话 | 远程会话碰不到你本机的剪贴板、浏览器、图形界面，会在说明里点出来 |
| 是不是在容器里 | 容器里多半没有 systemd，不会给 `systemctl` |

只说是与否，不带用户名和主机名。判断依据：有效用户 id、PATH 上有没有 `sudo`、`SSH_CONNECTION` / `SSH_TTY` / `SSH_CLIENT`、`/.dockerenv` / `/run/.containerenv` / `container` / `KUBERNETES_SERVICE_HOST`。

### 不敲 `@c`：`--natural`（只有 zsh，默认不开）

```bash
eval "$(xiaoyu term init zsh --natural)"
```

开了之后，在提示符上直接敲一句话回车就行：

```text
~/proj % 找出当前目录下大于 100M 的文件，按大小倒序        ← 敲的是这一行
~/proj % @c -- '找出当前目录下大于 100M 的文件，按大小倒序'   ← 回车的一刻它被改写成这样
列出大于 100M 的文件并按大小倒序
~/proj % find . -type f -size +100M -exec du -h {} + | sort -rh█
```

改写后的那一行留在屏幕和历史里，所以哪一行被转走了一眼看得出。之后与手敲 `@c` 完全一样。

**什么样的行会被转走**——两条都满足才算，往保守的方向错：

1. 整行里有非 ASCII 字符（中文、日文……）；
2. 第一个词不是任何命令、别名、函数、保留字，不是目录，也不带 shell 语法字符（`/` `=` `$` 引号 `\` 括号 `<` `>` `|` `&` `;` `!` `#` `~` `%`）。

所以真命令永远照常执行，哪怕参数是中文（`echo 你好`、`git commit -m "修复"`）。转不走、仍要写 `@c` 的有两种：

- **以命令名开头的句子**：`git 怎么回滚上一次提交` 第一个词是真命令，shell 会真的去跑 `git`。
- **纯英文的句子**：`show me big files` 和敲错的命令分不清，交给 shell 报 command not found。

顺带的好处：整句话进了单引号，里面的 `;` `|` `$HOME` `?` 括号都不会被 shell 解释，不用再走「只敲 `@c` 回车」那条路。想让某一行绕过判定，行首加一个 `\`。

**它包了一层回车键**（zsh 的 `accept-line`），这是默认不开的原因。原来的 `accept-line` 留在调用链上：先于它或后于它包回车键的别的东西照常被调到（按两种先后顺序测过），但没有拿真实的第三方插件逐个验证——开了之后回车行为不对，先关掉它排查。当前终端里立刻停：`zle -A __xiaoyu_term_natural_next accept-line`。

bash / fish / PowerShell 没有这个开关（`term init bash --natural` 会直接报错）：bash 没有在 shell 解析之前拿到整行的干净办法。

## 具名会话

默认每个终端一个随机 id（`term-<8 位>`），关掉终端这段对话就留在历史里：回到第一次 `@x` 时所在的目录敲 `xiaoyu resume` 就列得出来（在别的目录用 `xiaoyu resume --all`），选中后进交互界面接着聊。要多个终端共用、或关掉重开接着聊：

```bash
eval "$(xiaoyu term init zsh --name work)"     # 会话 id 固定为 term-work
```

同名会话同一时刻只能被一个进程写：两个终端同时 `@x` 时后一个会被告知会话正被占用。

## `--command-not-found`

```bash
eval "$(xiaoyu term init zsh --command-not-found)"
```

敲错的命令整行交给模型（`gti status` → 小羽告诉你是 `git status`）。**默认不开**：每个 typo 都打一次模型太费钱；PowerShell 下只接人在提示符敲的查找，脚本内部与模块自动加载的探测不接。

## 备用入口 `term log`

钩子不方便用内建追加的环境（自定义 shell、远程 wrapper），可以自己调：

```bash
xiaoyu term log "make test"
```

它走快路径（不导入 agent/tools），但仍是一次 Python 启动；日常用钩子，别把它挂到每条命令上。

## 隐私与文件

- **只记命令文本和退出码，不记输出。** 想让模型看到输出，让它自己重跑，或 `cmd 2>&1 | @x …` 当管道材料给它。
- **交给模型之前脱敏**：`Authorization: …`、`Bearer …`、`sk-…` / `ghp_…` 这类已知前缀的令牌、URL 里的 `user:pass@`、`token=…` / `password: …` 这类键值、`--password x` / `--token x` 这类旗标值、`XXX_SECRET_KEY=…` 这类环境变量赋值、mysql 系的 `-p密码`、`sshpass -p`、`curl -u user:pass` 都换成 `[REDACTED]`。脱敏是模式匹配，不认识的形态会漏——敲过明文密码的话自己留个心。
- **pending 文件**：`<配置目录>/term/<会话id>.pending`（macOS/Linux `~/.config/xiaoyu/term/`，Windows `%APPDATA%\xiaoyu\term\`），命令开跑前一行 `时间\t目录\t命令`，跑完后一行 `=时间\t退出码`（用开跑时间认领是哪条命令的），记的是脱敏**前**的原文，目录权限 0700。上限 500 条命令 / 256 KB，超了只留最新的。
- **会话文件**：`<配置目录>/sessions/term/`，与 `--session-id` 同一种格式；`xiaoyu resume` 按第一次提问时所在的目录列出它，`--all` 全列。
- 自己的 `@x …`、`@c …` 与 `xiaoyu term …` 不记。
- **`@c` 的两个文件**（都在 `<配置目录>/term/`）：`environment-<shell>.json` 是本机环境画像；`<会话id>.recall` 是这个终端最近 4 次 `@c` 的需求与命令（追问用，原文，30 分钟后不再带给模型）。`@c` 发给模型的是：环境画像、当前目录、会话处境（是不是 root / 有没有 sudo / 是不是 SSH / 是不是容器，不含用户名与主机名）、最近 12 条命令（脱敏后，只看不取）、最近几次 `@c`、你的需求，以及管道内容（有的话，最多 16000 字符）。

**关掉**：从启动文件删掉那一行，开个新终端即可。当前终端里要立刻停：zsh `add-zsh-hook -d preexec __xiaoyu_term_preexec; add-zsh-hook -d precmd __xiaoyu_term_precmd`、bash `trap - DEBUG`、fish `functions -e __xiaoyu_term_preexec __xiaoyu_term_postexec`、PowerShell 恢复 `$function:prompt = $function:__xiaoyu_term_prev_prompt`。（命令行不再记，退出码的钩子也就无事可做。）

## 各 shell 的边角

- **bash**：用的是 `DEBUG` trap，会顶掉你自己设的 `DEBUG` trap（有的话）。整行命令从 `history 1` 取，`HISTCONTROL=ignorespace` 下以空格开头的命令取不到整行，退回记管道里的第一段。需要 bash 4+（`EPOCHSECONDS` 没有时退回 `date`）。记退出码的函数排在 `PROMPT_COMMAND` 最前面并把 `$?` 原样传下去，你自己的提示符命令读到的 `$?` 不变。对不上通配符的词 bash 默认原样保留，`@x 为什么?` 能直接问；设了 `failglob` / `nullglob` 的话走不带问题的 `@x`。`@c` 给的命令进的是历史（`history -s`），关了历史（`set +o history`）就只能照着打出来的那行自己敲。
- **zsh**：`preexec` 拿到的就是整行，最省心。`@x` / `@c` 是别名，`setopt no_aliases` 的环境里用不了，改调 `__xiaoyu_term_ask` / `__xiaoyu_term_command`。`@c` 靠 `print -z` 把命令放上提示符，只在交互式 shell 里有意义。`--natural` 只在交互式 shell 里挂回车键；判定时把目录名一律当命令放行，不看你开没开 `autocd`。
- **fish**：没有内建的 epoch，时间戳那一下会 fork 一次 `date`。fish 3.x 里 `?` 还是通配符，问题带问号时走不带问题的 `@x`。没有 `@c`。
- **PowerShell**：没有 `@c`。命令在跑完、下一个提示符出现前记下，对 `x 问题` 来说一样及时；不包 PSReadLine 的回车键，不会动你的键位。退出码取自 `$?` 与 `$LASTEXITCODE`：成功记 0，原生命令失败记它的退出码，cmdlet 失败没有数字、记 1。
