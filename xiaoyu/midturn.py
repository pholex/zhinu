"""运行期输入行：按键字节流 → 草稿编辑动作（纯逻辑，不碰终端、不依赖 TUI 库）。

模型作答期间主线程被占住，输入由一个后台线程逐键读（终端在 cbreak 模式，
见 tui.SteerPoller）。读到的字节交给这里的 `Draft`：它只维护一行草稿并告诉
调用方"用户按了回车"或"用户要撤回"，不执行任何动作——读线程因此保持很薄，
解析规则也能脱离终端单测。

刻意只做追加式编辑：输入、退格、Ctrl-U 清行、Ctrl-W 删词、Esc。没有光标移动
——草稿画在活区里，那一行每帧重绘、没有真正的终端光标可停；方向键这类序列
整个吞掉，不让残片漏进草稿。要精修就等本轮结束，草稿会预填回完整的输入行。

三处要等"输入静下来"才能判定的歧义，统一由 `settle()` 收尾：

- 单独一个 ESC 是 Esc 键，后面紧跟字节则是转义序列的开头；
- 行尾的回车是提交，后面紧跟数据则是粘贴里的换行（终端没透传 bracketed
  paste 时，粘贴的多行就是这样到达的——逐行提交会把一段话拆成 N 条插话）；
- 半截转义序列：静下来还没等到结尾，按残片丢弃。
"""

from __future__ import annotations

import codecs

ESC = "\x1b"
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"

#  动作：("submit", 正文) 用户按了回车；("retract", "") 草稿为空时按了 Esc
Action = tuple[str, str]

_ERASE = ("\x7f", "\x08")
_KILL_LINE = "\x15"
_KILL_WORD = "\x17"


class Draft:
    """一行草稿 + 增量按键解析。`text` 可被别的线程只读地取用（整串替换，不就地改）。"""

    def __init__(self) -> None:
        self.text = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        #  还判定不了的尾巴：半截转义序列、行尾的回车、粘贴结束标记的前缀
        self._pending = ""
        self._pasting = False

    @property
    def unsettled(self) -> bool:
        """有等着"静下来"才能判定的尾巴：调用方应短等一个窗口后调 settle()。"""
        return bool(self._pending) and not self._pasting

    def feed(self, data: bytes) -> list[Action]:
        buf = self._pending + self._decoder.decode(data)
        self._pending = ""
        actions: list[Action] = []
        i = 0
        while i < len(buf):
            if self._pasting:
                i = self._paste(buf, i)
                continue
            ch = buf[i]
            if ch == ESC:
                step = self._escape(buf, i, actions)
                if step == 0:
                    self._pending = buf[i:]
                    break
                i += step
            elif ch in "\r\n":
                if ch == "\r" and buf[i + 1 : i + 2] == "\n":
                    i += 1
                if i == len(buf) - 1:
                    self._pending = buf[i]
                    break
                self.text += "\n"
                i += 1
            else:
                self._key(ch)
                i += 1
        return actions

    def settle(self) -> list[Action]:
        """输入静下来了：把歧义的尾巴按"后面没有东西"判定。"""
        if self._pasting:
            return []
        pending, self._pending = self._pending, ""
        actions: list[Action] = []
        if pending == ESC:
            self._escape_key(actions)
        elif pending in ("\r", "\n"):
            self._submit(actions)
        return actions

    def take(self) -> str:
        """取走草稿（轮末预填回输入行用）。"""
        text, self.text = self.text, ""
        return text

    def _key(self, ch: str) -> None:
        if ch in _ERASE:
            self.text = self.text[:-1]
        elif ch == _KILL_LINE:
            self.text = ""
        elif ch == _KILL_WORD:
            self.text = self.text.rstrip()
            cut = max(self.text.rfind(" "), self.text.rfind("\n"))
            self.text = self.text[: cut + 1]
        elif ch == "\t" or ch >= " ":
            self.text += ch
        #  其余控制字符（Ctrl-A、Ctrl-O…）在运行期没有含义，吞掉

    def _escape(self, buf: str, i: int, actions: list[Action]) -> int:
        """处理 buf[i] 处的 ESC，返回吃掉的字符数；0 = 序列不完整，等后续数据。"""
        if i + 1 >= len(buf):
            return 0
        following = buf[i + 1]
        if following == "[":
            end = i + 2
            while end < len(buf) and "\x20" <= buf[end] <= "\x3f":
                end += 1
            if end >= len(buf):
                return 0
            if buf[i : end + 1] == PASTE_START:
                self._pasting = True
            return end + 1 - i
        if following == "O":
            return 3 if i + 2 < len(buf) else 0
        if following == ESC:
            #  连按两次 Esc：先算一次，第二个留给下一圈
            self._escape_key(actions)
            return 1
        if following in "\r\n":
            #  Alt-Enter：与输入行同义，换行而不提交
            self.text += "\n"
            return 2
        return 2  # Alt+字符：运行期没有含义

    def _paste(self, buf: str, i: int) -> int:
        end = buf.find(PASTE_END, i)
        if end >= 0:
            self._insert_pasted(buf[i:end])
            self._pasting = False
            return end + len(PASTE_END)
        #  结束标记可能被读边界切开：尾巴若是它的前缀就先留着
        keep = 0
        for size in range(min(len(PASTE_END) - 1, len(buf) - i), 0, -1):
            if PASTE_END.startswith(buf[len(buf) - size :]):
                keep = size
                break
        self._insert_pasted(buf[i : len(buf) - keep])
        self._pending = buf[len(buf) - keep :] if keep else ""
        return len(buf)

    def _insert_pasted(self, chunk: str) -> None:
        chunk = chunk.replace("\r\n", "\n").replace("\r", "\n")
        self.text += "".join(ch for ch in chunk if ch in "\n\t" or ch >= " ")

    def _escape_key(self, actions: list[Action]) -> None:
        if self.text:
            self.text = ""
        else:
            actions.append(("retract", ""))

    def _submit(self, actions: list[Action]) -> None:
        lines = self.text.split("\n")
        self.text = ""
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            actions.append(("submit", "\n".join(lines)))
