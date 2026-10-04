# 终端集成：在自己的 shell 里 `@x`

不进 REPL。你照常在 shell 里敲命令，卡住了就 `@x 问题`——小羽带着**你刚跑过的那些命令和它们的退出码**回答，并且续写同一个会话，可以接着追问。

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

| shell | 放进启动文件的那一行 |
|---|---|
| zsh | `~/.zshrc`：`eval "$(xiaoyu term init zsh)"` |
| bash | `~/.bashrc`：`eval "$(xiaoyu term init bash)"` |
| fish | `~/.config/fish/config.fish`：`xiaoyu term init fish \| source` |
| PowerShell | `$PROFILE`：`Invoke-Expression (xiaoyu term init powershell \| Out-String)` |

脚本做三件事：

1. 导出 `XIAOYU_TERM_SESSION`（这个终端的会话 id）和 `XIAOYU_TERM_PENDING`（记命令的文件）；
2. 定义 `@x` / `@xiaoyu`（zsh 里是 `noglob` 别名，问题里的 `?` `*` `[` 不会被当通配符；PowerShell 里 `@` 是 splatting 语法当不了命令名，改叫 `x` / `Ask-Xiaoyu`）；
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

## 具名会话

默认每个终端一个随机 id（`term-<8 位>`），关掉终端这段对话就留在历史里（`xiaoyu resume --all` 还能找到）。要多个终端共用、或关掉重开接着聊：

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
- **会话文件**：`<配置目录>/sessions/term/`，与 `--session-id` 同一种格式，`xiaoyu resume --all` 可见。
- 自己的 `@x …` 与 `xiaoyu term …` 不记。

**关掉**：从启动文件删掉那一行，开个新终端即可。当前终端里要立刻停：zsh `add-zsh-hook -d preexec __xiaoyu_term_preexec; add-zsh-hook -d precmd __xiaoyu_term_precmd`、bash `trap - DEBUG`、fish `functions -e __xiaoyu_term_preexec __xiaoyu_term_postexec`、PowerShell 恢复 `$function:prompt = $function:__xiaoyu_term_prev_prompt`。（命令行不再记，退出码的钩子也就无事可做。）

## 各 shell 的边角

- **bash**：用的是 `DEBUG` trap，会顶掉你自己设的 `DEBUG` trap（有的话）。整行命令从 `history 1` 取，`HISTCONTROL=ignorespace` 下以空格开头的命令取不到整行，退回记管道里的第一段。需要 bash 4+（`EPOCHSECONDS` 没有时退回 `date`）。记退出码的函数排在 `PROMPT_COMMAND` 最前面并把 `$?` 原样传下去，你自己的提示符命令读到的 `$?` 不变。对不上通配符的词 bash 默认原样保留，`@x 为什么?` 能直接问；设了 `failglob` / `nullglob` 的话走不带问题的 `@x`。
- **zsh**：`preexec` 拿到的就是整行，最省心。`@x` 是别名，`setopt no_aliases` 的环境里用不了，改调 `__xiaoyu_term_ask`。
- **fish**：没有内建的 epoch，时间戳那一下会 fork 一次 `date`。fish 3.x 里 `?` 还是通配符，问题带问号时走不带问题的 `@x`。
- **PowerShell**：命令在跑完、下一个提示符出现前记下，对 `x 问题` 来说一样及时；不包 PSReadLine 的回车键，不会动你的键位。退出码取自 `$?` 与 `$LASTEXITCODE`：成功记 0，原生命令失败记它的退出码，cmdlet 失败没有数字、记 1。
