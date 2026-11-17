# 电脑操作（computer_use）实验

让小羽读本机 macOS 应用的界面、点按键入——"界面上才能做的事"。两部分：

- **原生辅助进程**「Xiaoyu Computer Use.app」（`macos/Service.swift`）：无窗口的 agent
  进程，持有系统的「辅助功能」与「屏幕录制」授权，把无障碍树、窗口截图、输入合成
  暴露成本地 socket 上的 JSON-RPC。它不做任何决策。
- **内置工具** `computer_use`（`xiaoyu/computer_use.py`）：辅助进程的客户端；审批、
  节奏（动作后等界面稳定再读）、树的增量回传、截图回灌模型都在这边。

## 构建与安装

需要 macOS 14+ 与 Xcode Command Line Tools。在 lab 工作树根目录：

```bash
.venv/bin/python labs/computer-use/macos/build.py --install
```

构建脚本只调用系统 Swift 编译器，复制到 `~/Applications/`（加 `--link ~/.local/bin` 顺便装
命令行客户端 `xiaoyu-cua`，见下节）。签名优先用钥匙串里的开发者
证书（Developer ID / Apple Development）：授权按签名的指定要求记，证书签名的要求是
"bundle id + 证书"，重建不丢授权；没有证书时退回 ad-hoc（`--sign -`），那样每次重建都要
重新勾一次权限。第一次启动时系统会弹两个授权对话框（辅助功能、屏幕录制），到
「系统设置 → 隐私与安全性」里为「Xiaoyu Computer Use」勾上即可。

**服务必须经 LaunchServices 启动**（小羽用 `open -g` 拉起；手动也用 `open`）。从终端直接
执行二进制，系统会把授权归到终端名下：无障碍读取看似可用，合成的键鼠事件却发不出去。

装好后新开的 `xiaoyu` 会话工具表里就有 `computer_use`（`XIAOYU_ENABLE_COMPUTER_USE=0`
关闭）。查找顺序：`XIAOYU_COMPUTER_USE_APP` 点名 → `~/Library/Application Support/xiaoyu/computer-use/`
（给桌面宿主代为安装用）→ `~/Applications` → `/Applications` → 按 bundle id 问系统的应用登记
（放在任何位置都行，Spotlight 索引到或启动过一次即可；这一步要几百毫秒到几秒，结果缓存一分钟）。辅助进程按需拉起；没有客户端连着且
30 分钟没有请求就自动退出（`defaults write app.xiaoyu.computer-use idleExitMinutes -int 0`
关掉自动退出），手动关用 `pkill -x XiaoyuComputerUse`——收到终止信号时它会先把临时切换的
输入源切回、把拖拽中按着的鼠标键抬起、删掉 socket 文件再退。

## 不经小羽，直接在命令行用

同一个可执行文件带参数启动就是协议的命令行客户端，不持有授权、不做审批，只把结果打到
标准输出。`build.py --install --link ~/.local/bin` 会把它软链成 `xiaoyu-cua`（目录要在
PATH 里）；服务没起时它会经 `open -g` 拉起 app 再连，`--no-launch` 则直接报错。

```bash
xiaoyu-cua ping                                  # 服务在不在、两项授权齐不齐
xiaoyu-cua apps                                  # 运行中的应用
xiaoyu-cua state 计算器 --screenshot /tmp/c.png   # 无障碍树（元素带索引）+ 截图存文件
xiaoyu-cua click 计算器 6                         # 按索引点；坐标写 x,y；--right / --count 2 / --mod cmd
xiaoyu-cua type 文本编辑 "你好" --append           # 文本为 - 时读标准输入
xiaoyu-cua key 计算器 cmd+c
xiaoyu-cua paste 文本编辑 - --format markdown < README.md
xiaoyu-cua do 计算器 click element=6 --state      # 直接拼 perform 参数；--state 动作后再打一遍树
xiaoyu-cua --help                                # 全部子命令
```

`--json` 原样打印服务返回；退出码 0 成功、1 服务报错（stderr 带错误码与文案）、2 用法错、
3 连不上服务。它没有小羽那层按会话审批与节奏控制：谁能跑它谁就能操作你授过权的界面，
与下文「socket 是一个授权借用面」是同一件事。

## 模型看到的

`state` 返回窗口的无障碍树文本 + 一张窗口截图：

```
[TextEdit（com.apple.TextEdit）窗口「未命名」 1200×800 坐标单位]
无障碍树（[n] 是元素索引，@x,y 是中心坐标，actions: 列出可用的次级动作）：
window "未命名"
  [12] textArea value="hello" (focused,editable) @600,420
  toolbar
    [3] button "样式" actions:showMenu @85,52
menuBar
  [40] menuBarItem "文件" @63,11
```

- 索引在同一应用内尽量稳定（按父路径、角色、同角色序号与标识记身份），所以后续
  `state` 默认只回"相对上次的变化"，未列出的元素索引照旧；变化太大自动退回整棵。
  只挪了位置或改了层级的行不算变化。50 次 `state` 没再出现的元素身份会被忘掉（网页
  换页、列表翻页会不停造新身份），再出现拿新号；应用退出后它的索引簿整本丢弃。
- 表格、列表、滚动区只列当前可见的那部分子元素：几千行全走一遍既慢又把节点预算
  （1500）耗光，滚出去的行也点不着。每个节点的属性一次取齐（一次进程间往返），整棵树
  另有 8 秒时间预算，超了标 `truncated`。
- 应用可以按显示名、英文 .app 文件名（显示名本地化成「访达」时 `Finder` 照样找得到）、
  bundle id 或完整路径指名。
- 坐标统一是"窗口坐标单位"：窗口左上角为原点，截图像素与之一一对应（大窗口按比例
  缩小，截图跟着缩），模型看图报的点直接可用。
- 每个动作执行后自动带回最新状态，省一次往返，也逼着模型基于新树决定下一步。

## 动作如何落地

| 动作 | 优先路径 | 退路 |
|---|---|---|
| click 元素 | 无障碍 `AXPress`（不动鼠标、不抢焦点） | 坐标点击 |
| type 到元素 | 可设值的编辑框直接 `AXSetValue`（整体替换） | 点一下聚焦，再合成键盘事件 |
| set_value / action | 无障碍属性与动作 | 无 |
| 坐标 click / key / scroll / drag / 裸 type | 把应用激活到前台，合成输入事件（公开 API） | 无 |

合成输入会打断用户正在用的窗口——这是用公开 API 的代价。工具描述里要求模型在用户
正在操作电脑时先确认。

## 用户介入

用户随时可能在动自己的鼠标键盘，辅助进程不能和人抢。不装事件监听（那要额外的
「输入监控」权限），靠两件事区分人与我们：

- 我们投递的每个事件都记时刻；系统报告的最近一次鼠标 / 键盘 / 滚轮事件若不落在这些
  时刻附近，就是人发的。合成输入前先看最近 1.2 秒内有没有这种事件，有就拒绝（-32012，
  动作没执行），由模型停下来问用户。
- 多步动作（键入分块、拖拽分步、滚动分步、多次点击）之间检查光标还在不在我们放的
  位置，被挪走就中止剩余步骤（-32012，可能只做了一部分）。
- 点击 / 滚动做完把光标还回原处（拖拽不还）；光标已被挪走或用户刚有活动就不碰。

无障碍动作（元素点击、设值）不经键鼠，不受此限。

已知盲区：键入进行中我们自己每十几毫秒发一个按键事件，系统计时被它们盖住，用户此时
物理敲键盘察觉不到（会和合成的字符交错进当前应用）；只有事件监听（需「输入监控」
权限）能看见，刻意不装。鼠标被挪动仍能察觉并中止。

## 不抢前台：做到哪一步

系统给"不切前台地给某个进程送事件"留了公开 API（按 pid 投递事件），实测边界：

| 场景 | 结果 |
|---|---|
| 元素点击 / 设值 / 次级动作（无障碍） | 从不抢前台，始终可用 |
| 键入到后台应用的焦点输入框 | **可用**：先经无障碍在插入点插入文本（确定能进），不行再把键盘事件投给进程（AppKit 文本视图能收） |
| 坐标点击 / 滚动 / 拖拽到后台应用 | **不可靠**：即便给事件标上目标窗口号，不少应用（含系统计算器）在非激活态直接忽略；弹菜单更要求前台 |

所以默认策略：`type` 在目标不在前台时自动走无障碍插入；坐标类动作仍先切前台。协议里的
`action.background: true` 是尽力而为的后台投递（事件投给进程、不切前台、不动光标），
留给知道目标应用能接的调用方，工具面暂不暴露。再往前一步只能靠私有接口，不做。

## 虚拟光标

元素点击、设值这些无障碍动作不动系统鼠标，用户看不出小羽在点哪里；坐标动作虽然动鼠标，
也和用户自己的鼠标没有区分。辅助进程因此自己画一个光标：一块不接鼠标事件、浮在所有
窗口之上（辅助技术专用层级，跨 Space、全屏里也显示）的透明面板，上面用 CoreAnimation
画一支白色箭头（尾部一个羽毛缺口，和图标呼应），尖端就是动作落点。

- 每个动作前先飞到目标：移动时沿方向轻微拉伸再弹回，看得出是"飞过去"而不是瞬移；
  按下时缩一下并荡开一圈涟漪。
- 空闲 1.5 秒淡出；侦测到用户介入（-32012）立即收起。
- 它只是提示，不参与任何判断；关掉：`defaults write app.xiaoyu.computer-use showCursor -bool false`
  （重启辅助进程生效）。
- 实现细节：面板图层用 AppKit 默认的左下角原点，不依赖图层翻转标志（视图会按自己的
  `isFlipped` 覆盖它）；热点距顶 36 点换算成距底坐标。

## 粘贴与选区

`paste` 走系统剪贴板：先把用户剪贴板的全部类型快照，写入要粘的内容（markdown 经系统
转成 HTML 并附纯文本回退，html 另附纯文本），目标切前台后发 cmd+v，等应用读完再把原
剪贴板还回去；中途用户自己复制了东西（changeCount 变了）就不覆盖。长文本、富文本用它
而不是逐字符键入。markdown 的标题、列表经系统转换会降级成带样式的段落，复杂排版给 html。

`select_text` 走无障碍 `AXSelectedTextRange`，不动键鼠、不抢前台：在元素（或应用焦点
元素）的文本里找子串，选中它或把光标放到它前后，同时把键盘焦点给该元素，之后的
`type` / `paste` 替换选中内容、`key Delete` 删除。网页里的编辑器不一定支持设置选区，
失败时按提示改用 click 定位加 shift+方向键。

## 输入法与键入

中文等输入法开着时，合成的按键会被当成拼码吞进候选框（键入没反应、Return 只是提交
候选）。合成键盘输入前临时把输入源切到 ASCII 键盘布局，敲完立刻切回原来的输入法；
本来就是键盘布局的不动。键入逐字符发送：一个事件里塞多个字符时，按键驱动的应用
（计算器、快捷键）只取第一个。键盘上有的字符用真实键码（含 shift 组合，如 `+`、`*`），
应用按键码判也能认；其余字符（中文等）走 unicode 串事件。

## 图标

`macos/AppIcon.png`（1024×1024 设计稿）存在时按尺寸缩放合成 .icns；没有就由
`MakeIcon.swift` 画一枚代码生成的占位图标。设计稿不入库也行，构建会自动退回占位。

## 协议

unix socket（默认 `~/Library/Application Support/xiaoyu/computer-use/ipc.sock`，两端都读
`XIAOYU_COMPUTER_USE_SOCKET`），每帧 = 4 字节小端长度 + UTF-8 JSON-RPC 2.0。

| 方法 | 参数 | 结果 |
|---|---|---|
| `ping` | — | `apiVersion`、`accessibility`、`screenCapture` 是否已授权 |
| `probe_activity` | — | 诊断：各类输入事件距今秒数与据此判断的"用户最近一次输入" |
| `probe_input` | `x`, `y`（可省） | 诊断：把鼠标挪到该屏幕坐标再读回，返回 `before` / `after` / `wanted`，用来判断合成事件是否生效 |
| `list_apps` | — | 运行中的普通应用 `{id,name,pid,active}` |
| `get_state` | `app`, `screenshot` | `app`、`window`、`space{width,height,scale}`、`tree`、`elements`、`truncated`、`screenshot{png_base64,width,height}` 或 `screenshot_error` |
| `perform` | `app`, `action{kind,…}` | `{ok, note}` |

`action.background: true`（可选，尽力而为的后台投递，见上节）。`action.kind`：`activate`（只切前台）、`click`（element 或 x/y；button、count 1–3、modifiers）、`double_click`、`type`
（text；可带 element；`append: true` 时不整体替换而是点进去追加键入）、`key`（key；`+` 本身
写 `+` 或 `cmd++`，带 shift 的符号如 `?` 可直接作主键）、`scroll`（element 或 x/y；direction、
pages 0.1–20）、`drag`（from_x/from_y/to_x/to_y）、`set_value`（element、value）、`action`
（element、name）、`paste`（text；format=text/markdown/html；可带 element）、`select_text`
（element 可省=焦点元素；text 子串，省略即全选；occurrence；mode=select/start/end）。
数值参数超出范围按边界夹取，非有限数拒绝。

错误码：-32001 未授权、-32002 找不到应用、-32003 应用名有歧义、-32004 元素索引过期、
-32005 没有窗口、-32006 不支持、-32007 屏幕已锁定、-32011 切不到前台（拒绝合成输入，
避免按键落进别的窗口）、-32012 用户正在操作（拒绝或中止合成输入）；文案直接可转给模型。

## 边界与刻意不做

- **socket 是一个授权借用面。** 辅助进程持有「辅助功能」与「屏幕录制」授权，而 socket
  只按文件权限（0600）和对端 uid 校验限制在"同一个用户"：这个用户下的任何进程，哪怕自己
  没有这两项授权，都能经它读窗口、截图、合成输入。这是把授权放进独立 .app 的固有代价。
  缓解：空闲自动退出缩短暴露窗口；不用时 `pkill` 或直接删掉 .app（授权随之失效）。
  不做 token 握手：同一用户的进程读得到任何 token，徒增复杂度。
- 只 macOS；Linux / Windows 没有辅助进程，工具不出现。
- 每次调用都经审批（可按会话放行），不按应用分级授权；动作的"后果"分级交给模型按
  工具描述里的确认纪律判断。
- 辅助进程不入 wheel：它是 Swift 编译产物，由用户在本机构建安装。

## 验证

```bash
PYTHONPATH="$PWD" .venv/bin/python -P -m unittest tests.test_computer_use -v   # 假服务，不碰真实应用
```

真机验收（改了 Service.swift 或工具面后在本机跑一次；会打开计算器与文本编辑，期间别动键鼠）：

```bash
.venv/bin/python -P labs/computer-use/smoke.py            # 18 步：授权、每个动作、剪贴板恢复、错误路径、断连与超大数值不崩、命令行客户端
.venv/bin/python -P labs/computer-use/smoke.py -k paste   # 只跑名字含 paste 的步骤
```

退出码 0 全过、1 有失败、2 辅助进程没装或没授权。它不进 CI，也不是 unittest。
