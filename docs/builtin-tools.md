# 文件工具与后台事件

## 多文件补丁

`apply_patch` 接收 `changes` 数组，每项只有一个文件，最多 50 项：

```json
{"changes":[
  {"path":"src/example.py","operation":"update","diff":"@@ -1 +1 @@\n-old = 1\n+new = 2\n"},
  {"path":"note.txt","operation":"create","diff":"@@ -0,0 +1 @@\n+hello\n"},
  {"path":"obsolete.txt","operation":"delete"}
]}
```

`diff` 是不带 `---` / `+++` 文件头的 unified diff。hunk 的行号、行数和
上下文必须精确匹配；不猜测位置。末行没有换行时，使用标准的
`\ No newline at end of file` 标记。一次调用不能重复指定同一文件，符号链接
别名也算重复；不支持移动文件。

修改已有文件前先 `read_file`，删除要完整读过；读后版本变化则拒绝。保留已有
文件编码与换行。全部补丁先校验，通过后逐个落盘；这不是多文件事务，I/O 失败
时返回已完成、失败和未执行清单，不要盲目重发整批。路径沿用文件工具的解析规则，跟随符号链接；
删除作用于解析后的文件。文件改动参与 `/rewind`，回退恢复文件内容，不保证恢复
删除前的权限位等元数据。

### 批量精确替换

更新文件也可用 `edits` 代替 `diff`，无需计算行号：

```json
{"changes":[
  {"path":"a.py","operation":"update","edits":[
    {"old_str":"name = 'old'","new_str":"name = 'new'"},
    {"old_str":"count = 1","new_str":"count = 2"}
  ]},
  {"path":"b.py","operation":"update","edits":[
    {"old_str":"enabled = False","new_str":"enabled = True"}
  ]}
]}
```

每个文件最多 100 处替换。所有 `old_str` 都在该文件的**同一份原文**里匹配，
必须非空、唯一、互不重叠；不会拿前一处替换后的内容去匹配下一处。
不使用模糊匹配，多行替换须从行首开始并带上缩进。`edits` 仅用于 `update`，
不能与 `diff` 同传。沿用补丁的整批预检、逐路径审批、编码换行保留、产物和回退。

## 本地图片

`view_image` 把本地 PNG/JPEG/GIF/WEBP 交给模型，原图读取无需额外依赖：

```json
{"path":"screenshot.png"}
{"path":"screenshot.png","crop":[100,200,900,600],"max_size":1600}
```

裁剪与缩放需 `pip install 'xiaoyu-agent[image]'`。坐标是原始像素的
`[左,上,右,下]`（右、下不包含），先裁剪再按最长边等比缩小，不放大；
`max_size` 为 1–4096。不会隐式旋转 EXIF，动画变换取第一帧。
原文件不变，生成图进入已有媒体缓存，并沿用模型视觉能力检查与代读配置。
源文件最多 7 MiB，变换最多 4000 万像素，输出受媒体大小上限约束；
越界路径沿用读取审批。无视觉能力且未配置代读时不会声称已经看见图片。

## PDF 文字与页面图

安装可选依赖：

```sh
pip install 'xiaoyu-agent[pdf]'
```

`read_document` 始终可发现，未安装依赖时返回安装指引。

```json
{"path":"report.pdf","pages":"1-3,5","mode":"text"}
{"path":"report.pdf","pages":"2","mode":"image"}
```

默认第 1 页、文字模式。文字模式最多 20 页，保留页码；无文字时提示改用图片。
图片模式最多 4 页，彩色页面长边不超过 2000 像素，图片按请求页序交给模型。
单文件最多 50 MiB，单次文字最多 200000 字符；长输出沿用 `recall`。
扫描页不会自动 OCR，文字提取也不承诺复原复杂表格布局；图表和排版请看页面图。
加密 PDF 需要先由用户解密，工具没有密码参数。当前仅支持 PDF。

## 交互任务

```json
{"command":"python -u -i","interactive":true,"tty":true}
```

`bash` 返回 task ID；`interactive=true` 已包含后台运行，无须另外指定
`run_in_background`。普通输入管道支持各平台；`tty=true` 需要 POSIX 平台，
分配独立伪终端，适合需要 `isatty()` 的 REPL。不会接管用户输入终端，且不承诺
完整交互 shell 的作业控制。Windows 使用普通管道。

```json
{"task_id":"task-1","text":"print(1 + 2)\n"}
{"task_ids":["task-1"],"timeout":1}
{"task_id":"task-1","text":"","eof":true}
```

以上分别调用 `task_input`、`task_output`、`task_input`。输入原样发送，不自动
补换行；每次最多 4096 UTF-8 字节。EOF 后不再接受普通输入；伪终端 EOF 为
终端控制字符，程序是否退出取决于终端模式和程序自身。
POSIX 上单独发送 `"\u0003"` 给任务进程组发中断信号；彻底结束用 `kill_task`。
输入阻塞会有界返回，管道写阻塞超时则终止任务，并提示部分输入可能已送达。

启动交互任务和每次输入都明确审批，密码和密钥不走模型。`--unattended` 可用于
隔离的无人值守环境。任务只属于当前会话进程，结束会话会回收，resume 不能接回
旧进程。普通后台命令仍默认关闭 stdin，行为不变。

## 等待事件

`wait_for_event` 等待当前会话中的后台任务完成、monitor 通知或协作消息：

```json
{"timeout":60}
```

默认 60 秒，范围 0–600；0 仅检查。有尚未送达的事件立即返回，用户插话或取消
会提前结束。通知内容仍经工具结果送达，协作消息和插话仍在工具批次结束的边界
送达，不重复投递或冒充用户输入。`wake=false` 的公告不会唤醒等待。

这个工具不启动任务、不自动重试、不等待已经送达的旧事件。超时后可以继续工作；
要查具体任务的状态和日志用 `task_output`。进程重启后不能等旧后台任务。
