"""电脑操作的真机验收：对着装好的「Xiaoyu Computer Use.app」把每个动作跑一遍。

不是单元测试（tests/ 里的假服务不碰真实应用），不进 CI；改了 Service.swift 或工具面之后
在本机跑一次。会打开计算器与文本编辑，期间别动鼠标键盘——辅助进程发现你在操作会拒绝合成
输入（-32012），脚本会等两秒重试几次。跑完关掉它开的文档（不保存）。

用法（仓库根目录）：
    .venv/bin/python -P labs/computer-use/smoke.py            # 全部
    .venv/bin/python -P labs/computer-use/smoke.py -k paste   # 只跑名字含 paste 的步骤
退出码：全过 0，有失败 1，辅助进程没装或没授权 2。
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from xiaoyu import computer_use as cu  # noqa: E402

CALC = "Calculator"
EDIT = "TextEdit"
MARKER = "原剪贴板内容（smoke）"


class Smoke:
    def __init__(self):
        self.client = cu.Client()
        self.results: list[tuple[str, bool, str]] = []

    # -- 小工具 --

    def perform(self, app: str, **action):
        """-32012（用户正在操作）最多重试 8 次，其余错误原样抛。"""
        for _ in range(8):
            try:
                return self.client.call("perform", app=app, action=action)
            except cu.ServiceError as exc:
                if exc.code != -32012:
                    raise
                print("    （检测到你在操作，等 2 秒重试）")
                time.sleep(2)
        raise RuntimeError("用户一直在操作，放弃")

    def tree(self, app: str) -> str:
        return self.client.call("get_state", app=app, screenshot=False)["tree"]

    def index(self, app: str, role_and_label: str) -> int:
        for line in self.tree(app).splitlines():
            if role_and_label in line and line.strip().startswith("["):
                return int(line.strip().split("]")[0][1:])
        raise AssertionError(f"树里没有 {role_and_label}")

    def calc_value(self) -> str:
        for line in self.tree(CALC).splitlines():
            if "编辑字段" in line:
                return line.split('value="')[1].split('"')[0]
        raise AssertionError("计算器没有编辑字段")

    def edit_value(self) -> str:
        for line in self.tree(EDIT).splitlines():
            if "textArea" in line:
                #  值里可能有引号：取到 `" (flags)` / `" actions:` / `" @x,y` 之前
                match = re.search(r'value="(.*)" (?:\(|actions:|@)', line)
                return match.group(1) if match else ""
        raise AssertionError("文本编辑没有 textArea")

    def pid(self) -> str:
        return subprocess.run(["/usr/bin/pgrep", "-x", "XiaoyuComputerUse"], capture_output=True, text=True).stdout.strip()

    # -- 步骤 --

    def step_ping(self):
        info = self.client.call("ping")
        assert info.get("apiVersion") == cu.API_VERSION, info
        assert info.get("accessibility") is True, "辅助功能未授权"
        assert info.get("screenCapture") is True, "屏幕录制未授权"

    def step_list_apps(self):
        apps = self.client.call("list_apps")["apps"]
        assert isinstance(apps, list) and apps, apps

    def step_state_and_screenshot(self):
        state = self.client.call("get_state", app=CALC, screenshot=True)
        assert 'button "7"' in state["tree"], state["tree"][:300]
        png = base64.b64decode(state["screenshot"]["png_base64"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "截图不是 PNG"
        assert state["space"]["width"] == state["screenshot"]["width"], "截图尺寸与坐标单位不一致"

    def step_finder_by_english_name(self):
        state = self.client.call("get_state", app="Finder", screenshot=False)
        assert state["app"]["id"] == "com.apple.finder", state["app"]

    def step_click_type_key(self):
        self.perform(CALC, kind="key", key="Escape")
        note = self.perform(CALC, kind="click", element=self.index(CALC, 'button "7"'))["note"]
        assert "无障碍" in note, note
        self.perform(CALC, kind="type", text="+5")
        self.perform(CALC, kind="key", key="Return")
        assert self.calc_value() == "12", self.calc_value()

    def step_plus_chord(self):
        self.perform(CALC, kind="key", key="Escape")
        self.perform(CALC, kind="type", text="7")
        self.perform(CALC, kind="key", key="+")
        self.perform(CALC, kind="type", text="5")
        self.perform(CALC, kind="key", key="Return")
        assert self.calc_value() == "12", self.calc_value()

    def step_click_xy(self):
        self.perform(CALC, kind="click", x=88, y=157)  # 清除 / AC
        assert self.calc_value() == "0", self.calc_value()

    def step_action_show_menu(self):
        note = self.perform(CALC, kind="action", element=self.index(CALC, 'button "模式"'), name="showMenu")["note"]
        assert "ShowMenu" in note, note
        self.perform(CALC, kind="key", key="Escape")

    def step_open_text_document(self):
        subprocess.run(["/usr/bin/osascript", "-e", f'tell application "{EDIT}" to activate',
                        "-e", f'tell application "{EDIT}" to make new document'], check=True, stdout=subprocess.DEVNULL)
        time.sleep(1.5)
        self.perform(EDIT, kind="activate")
        #  偏好里新文档是纯文本时，markdown / html 粘贴只会取字符串类型：先切到富文本
        rich = subprocess.run(["/usr/bin/defaults", "read", "com.apple.TextEdit", "RichText"],
                              capture_output=True, text=True).stdout.strip()
        if rich == "0":
            self.perform(EDIT, kind="key", key="cmd+shift+t")
            time.sleep(0.6)

    def step_set_value_select_paste(self):
        area = self.index(EDIT, "textArea")
        self.perform(EDIT, kind="set_value", element=area, value="alpha beta gamma\n" * 3)
        note = self.perform(EDIT, kind="select_text", text="beta", occurrence=2)["note"]
        assert "beta" in note, note
        subprocess.run(["/usr/bin/pbcopy"], input=MARKER.encode(), check=True)
        self.perform(EDIT, kind="paste", text="BETA")
        time.sleep(0.5)
        assert self.edit_value() == "alpha beta gamma⏎alpha BETA gamma⏎alpha beta gamma⏎", self.edit_value()
        restored = subprocess.run(["/usr/bin/pbpaste"], capture_output=True, text=True).stdout
        assert restored == MARKER, f"剪贴板没恢复：{restored!r}"

    def step_paste_markdown(self):
        self.perform(EDIT, kind="select_text")
        self.perform(EDIT, kind="key", key="Delete")
        self.perform(EDIT, kind="paste", text="# 大标题\n\n- 甲\n- 乙\n\n**粗** 普通\n", format="markdown")
        time.sleep(0.6)
        value = self.edit_value()
        assert value.startswith("大标题⏎") and "\t•\t甲" in value and "粗 普通" in value, value

    def step_type_symbols_and_crlf(self):
        self.perform(EDIT, kind="select_text")
        self.perform(EDIT, kind="key", key="Delete")
        self.perform(EDIT, kind="type", text="Hello, World! ?@#$%^&*()_+{}|:\"<>~ az\r\nline2")
        time.sleep(0.4)
        #  富文本模式下文本编辑会把直引号换成弯引号（它的"智能引号"），不是键入错
        value = self.edit_value().translate(str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"}))
        assert value == "Hello, World! ?@#$%^&*()_+{}|:\"<>~ az⏎line2", value

    def step_double_click_drag_scroll(self):
        self.perform(EDIT, kind="double_click", x=40, y=60)
        self.perform(EDIT, kind="drag", from_x=20, from_y=60, to_x=120, to_y=60)
        self.perform(EDIT, kind="scroll", element=self.index(EDIT, "textArea"), direction="down", pages=1)

    def step_error_paths(self):
        expect = {
            ("nope",): -32602, ("type",): -32602, ("key", "cmd+???"): -32602, ("xy", -5, 5): -32602,
            ("element", 999_999): -32004,
        }
        actions = {
            ("nope",): {"kind": "nope"}, ("type",): {"kind": "type"}, ("key", "cmd+???"): {"kind": "key", "key": "cmd+???"},
            ("xy", -5, 5): {"kind": "click", "x": -5, "y": 5}, ("element", 999_999): {"kind": "click", "element": 999_999},
        }
        for key, code in expect.items():
            try:
                self.client.call("perform", app=EDIT, action=actions[key])
            except cu.ServiceError as exc:
                assert exc.code == code, (key, exc.code, str(exc))
            else:
                raise AssertionError(f"{key} 没有被拒绝")

    def step_survives_bad_numbers(self):
        before = self.pid()
        self.perform(CALC, kind="scroll", pages=1e300)
        self.perform(CALC, kind="click", element=self.index(CALC, 'button "7"'), count=1e300)
        assert self.pid() == before, "辅助进程重启了（崩溃）"
        self.perform(CALC, kind="key", key="Escape")

    def step_survives_early_disconnect(self):
        before = self.pid()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(cu.socket_path()))
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "get_state",
                              "params": {"app": CALC, "screenshot": True}}).encode()
        sock.sendall(struct.pack("<I", len(payload)) + payload)
        sock.close()
        time.sleep(2)
        assert self.pid() == before, "客户端提前断开把辅助进程打死了"

    def step_command_line(self):
        """同一个可执行文件带参数 = 命令行客户端（不经 Python 客户端），输出与退出码要稳定。"""
        exe = str(cu.app_path() / "Contents" / "MacOS" / "XiaoyuComputerUse")

        def cli(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([exe, *args], capture_output=True, text=True, check=False)

        out = cli("ping")
        assert out.returncode == 0 and "已授权" in out.stdout, (out.returncode, out.stdout, out.stderr)
        out = cli("apps")
        assert out.returncode == 0 and "com.apple." in out.stdout, out
        shot = Path(tempfile.mkdtemp()) / "cli.png"
        out = cli("state", CALC, "--screenshot", str(shot))
        assert out.returncode == 0 and 'button "7"' in out.stdout and str(shot) in out.stdout, out.stdout[:300]
        assert shot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "CLI 存的截图不是 PNG"
        out = cli("state", CALC, "--json")
        assert out.returncode == 0 and set(json.loads(out.stdout)) >= {"tree", "elements", "space"}, out.stdout[:200]
        def cli_idle(*args: str) -> subprocess.CompletedProcess[str]:
            for _ in range(8):
                out = cli(*args)
                if "-32012" not in out.stderr:
                    return out
                print("    （检测到你在操作，等 2 秒重试）")
                time.sleep(2)
            raise RuntimeError("用户一直在操作，放弃")

        out = cli_idle("key", CALC, "Escape")
        assert out.returncode == 0, out.stderr
        out = cli("click", CALC, str(self.index(CALC, 'button "7"')), "--state")
        assert out.returncode == 0 and out.stdout.startswith("已执行 click") and 'button "7"' in out.stdout, out
        assert self.calc_value() == "7", self.calc_value()
        out = cli_idle("do", CALC, "key", "key=Escape")
        assert out.returncode == 0 and self.calc_value() == "0", (out, self.calc_value())
        #  退出码：1 服务报错、2 用法错、3 连不上
        out = cli("state", "不存在的应用xyz")
        assert out.returncode == 1 and "-32002" in out.stderr, out
        out = cli("click", CALC)
        assert out.returncode == 2 and "用法" in out.stderr, out
        out = cli("do", CALC, "click", "element=1e300")
        assert out.returncode == 1 and "-32004" in out.stderr, out
        out = cli("ping", "--socket", str(shot.parent / "nope.sock"), "--no-launch")
        assert out.returncode == 3, out
        out = cli("--help")
        assert out.returncode == 0 and "用法" in out.stdout, out
        shutil.rmtree(shot.parent, ignore_errors=True)

    def step_cleanup(self):
        subprocess.run(["/usr/bin/osascript", "-e", f'tell application "{EDIT}" to close every document saving no'],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.perform(CALC, kind="key", key="Escape")

    # -- 跑 --

    STEPS = [
        step_ping, step_list_apps, step_state_and_screenshot, step_finder_by_english_name,
        step_click_type_key, step_plus_chord, step_click_xy, step_action_show_menu,
        step_open_text_document, step_set_value_select_paste, step_paste_markdown, step_type_symbols_and_crlf,
        step_double_click_drag_scroll, step_error_paths, step_survives_bad_numbers, step_survives_early_disconnect,
        step_command_line, step_cleanup,
    ]

    def run(self, only: str | None) -> int:
        for step in self.STEPS:
            name = step.__name__.removeprefix("step_")
            if only and only not in name and name not in ("ping", "open_text_document", "cleanup"):
                continue
            started = time.monotonic()
            try:
                step(self)
            except Exception as exc:  # noqa: BLE001 - 一步失败不挡后面的步骤
                detail = f"{type(exc).__name__}: {exc}"
                if not isinstance(exc, (AssertionError, cu.ServiceError)):
                    detail += "\n" + traceback.format_exc()
                self.results.append((name, False, detail))
                print(f"  FAIL  {name}  ({time.monotonic() - started:.1f}s)\n    {detail}")
            else:
                self.results.append((name, True, ""))
                print(f"  PASS  {name}  ({time.monotonic() - started:.1f}s)")
        failed = [r for r in self.results if not r[1]]
        print(f"\n{len(self.results) - len(failed)}/{len(self.results)} 通过")
        return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("-k", dest="only", default=None, help="只跑名字含该子串的步骤（ping、开文档、cleanup 总是跑）")
    args = parser.parse_args()
    if not cu.available():
        print("辅助 app 没装（见 labs/computer-use/README.md）", file=sys.stderr)
        return 2
    smoke = Smoke()
    try:
        smoke.client.call("ping")
    except cu.ServiceError as exc:
        print(f"辅助进程不可用：{exc}", file=sys.stderr)
        return 2
    print("开始真机验收：期间请别动鼠标键盘\n")
    return smoke.run(args.only)


if __name__ == "__main__":
    sys.exit(main())
