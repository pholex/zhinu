"""小羽的 eval 任务集。

加新 case 的原则：
- 判据必须机械可判，不要主观评分。
- 每个 case 要能区分"真做对了"和"看起来像做对了"（比如靠 diff 规模抓整文件重写）。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from .harness import (
    Case,
    command_succeeds,
    file_contains,
    file_exists,
    file_not_contains,
    max_changed_lines,
    never_used_tool,
    no_tool_errors,
    nothing_written,
    python_snippet_ok,
    python_syntax_ok,
    tests_pass,
    transcript_contains,
    unchanged_except,
    used_tool,
)


def _write(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


# ---------- case 1：修 bug + 自己写测试跑通 ----------


def _setup_calc(root: Path) -> None:
    _write(
        root,
        "calc.py",
        """
        def add(a, b):
            return a + b


        def div(a, b):
            return a / b
        """,
    )


CASE_FIX_AND_TEST = Case(
    name="fix_and_test",
    description="修除零 bug、加类型注解、自己写测试并跑通",
    prompt=(
        "calc.py 里的 div 没处理除零。修掉它，给两个函数加上类型注解，"
        "然后写一个测试文件并实际跑通验证。"
    ),
    setup=_setup_calc,
    checks=[
        (
            "除零会抛异常、正常除法不受影响",
            python_snippet_ok(
                """
                import calc

                try:
                    calc.div(1, 0)
                except Exception:
                    pass
                else:
                    raise AssertionError("div(1, 0) 没有抛异常")

                assert calc.div(6, 3) == 2, calc.div(6, 3)
                assert calc.add(2, 3) == 5, calc.add(2, 3)
                """,
                note="除零行为",
            ),
        ),
        ("calc.py 语法正确", python_syntax_ok("calc.py")),
        ("加了类型注解", file_contains("calc.py", "->")),
        ("生成了测试文件", file_exists("test_*.py")),
        ("测试实际能跑通", tests_pass()),
        ("跑过命令验证", used_tool("bash")),
        ("没有反复试错", no_tool_errors(max_errors=1)),
    ],
)


# ---------- case 2：大文件里的定点修改 ----------


def _setup_http_client(root: Path) -> None:
    head = '''
    """内部 HTTP 客户端封装。"""

    import time
    import urllib.error
    import urllib.request

    DEFAULT_TIMEOUT = 10
    MAX_RETRIES = 4


    class HttpError(Exception):
        """请求失败。"""


    def _build_request(url, method, headers, body):
        request = urllib.request.Request(url, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        return request


    def fetch(url, method="GET", headers=None, body=None, timeout=DEFAULT_TIMEOUT):
        """带重试的请求。失败会指数退避后重试。"""
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                request = _build_request(url, method, headers, body)
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.read()
            except urllib.error.URLError as exc:
                last_error = exc
                time.sleep(1)
        raise HttpError(f"{url} 请求失败：{last_error}")
    '''
    filler = "\n\n".join(
        f'def helper_{n}(value):\n    """占位工具函数 {n}。"""\n    return value * {n}'
        for n in range(1, 26)
    )
    #  head 是缩进的三引号字符串，filler 不是——必须先各自 dedent 再拼，
    #  否则 textwrap.dedent 找不到公共前缀，head 的缩进会被原样写进文件。
    _write(root, "http_client.py", textwrap.dedent(head).lstrip("\n") + "\n\n" + filler + "\n")


CASE_TARGETED_EDIT = Case(
    name="targeted_edit",
    description="130 行文件里只改该改的那几行，不许整文件重写",
    prompt=(
        "http_client.py 的 fetch 文档说是指数退避，但实现里 time.sleep(1) 是固定间隔。"
        "改成指数退避（1、2、4 秒），并且最后一次尝试失败后不要再多睡一次。"
        "只改这一处，别动别的函数。"
    ),
    setup=_setup_http_client,
    checks=[
        ("固定 sleep(1) 已消失", file_not_contains("http_client.py", "time.sleep(1)")),
        ("语法正确", python_syntax_ok("http_client.py")),
        ("改动不超过 8 行", max_changed_lines("http_client.py", 8)),
        ("没碰其它文件", unchanged_except("http_client.py")),
        ("用了 str_replace", used_tool("str_replace")),
        ("没有整文件重写", never_used_tool("write_file")),
        (
            "最后一次不再等待",
            python_snippet_ok(
                """
                import time
                import urllib.error

                import http_client

                slept = []
                time.sleep = lambda seconds: slept.append(seconds)


                def boom(*args, **kwargs):
                    raise urllib.error.URLError("forced")


                http_client.urllib.request.urlopen = boom

                try:
                    http_client.fetch("http://example.invalid")
                except http_client.HttpError:
                    pass
                else:
                    raise AssertionError("全部重试失败后应该抛 HttpError")

                assert slept == [1, 2, 4], f"退避序列不对：{slept}"
                """,
                note="退避序列",
            ),
        ),
    ],
)


# ---------- case 3：只读任务，一个字都不许改 ----------


def _setup_readonly(root: Path) -> None:
    _write(
        root,
        "pkg/handlers_a.py",
        """
        def handle_login(event):
            return event


        def handle_logout(event):
            return event


        def helper(event):
            return event
        """,
    )
    _write(
        root,
        "pkg/handlers_b.py",
        """
        def handle_create(event):
            return event


        def handle_update(event):
            return event


        def handle_delete(event):
            return event
        """,
    )
    _write(
        root,
        "pkg/misc.py",
        """
        def handle_ping(event):
            return "pong"


        def handle_health(event):
            return "ok"


        def not_a_handler(event):
            return event
        """,
    )


CASE_READONLY_ANSWER = Case(
    name="readonly_answer",
    description="回答关于代码库的事实问题，且不许动任何文件",
    prompt=(
        "统计 pkg/ 目录下所有以 handle_ 开头的函数一共有多少个，只告诉我数字和它们分布在哪些文件。"
        "不要修改任何文件。"
    ),
    setup=_setup_readonly,
    checks=[
        ("答对数量 7", transcript_contains("7")),
        ("一个文件都没动", nothing_written()),
        ("没调用 write_file", never_used_tool("write_file")),
        ("没调用 str_replace", never_used_tool("str_replace")),
    ],
)


# ---------- case 4：跨文件重命名 ----------


def _setup_rename(root: Path) -> None:
    _write(
        root,
        "core.py",
        """
        USERS = {1: "alan", 2: "lixing"}


        def fetch_user(user_id):
            \"\"\"按 id 取用户名。\"\"\"
            return USERS.get(user_id)
        """,
    )
    _write(
        root,
        "api.py",
        """
        from core import fetch_user


        def get_profile(user_id):
            name = fetch_user(user_id)
            if name is None:
                raise KeyError(f"no such user: {user_id}")
            return {"id": user_id, "name": name}
        """,
    )
    _write(
        root,
        "test_core.py",
        """
        import unittest

        from api import get_profile
        from core import fetch_user


        class TestUsers(unittest.TestCase):
            def test_fetch(self):
                self.assertEqual(fetch_user(1), "alan")

            def test_profile(self):
                self.assertEqual(get_profile(2)["name"], "lixing")

            def test_missing(self):
                with self.assertRaises(KeyError):
                    get_profile(99)


        if __name__ == "__main__":
            unittest.main()
        """,
    )


CASE_MULTI_FILE_RENAME = Case(
    name="multi_file_rename",
    description="跨 3 个文件重命名函数，改完测试还得过",
    prompt=(
        "把 fetch_user 重命名为 load_user，全仓库都要改干净（包括调用方和测试），"
        "改完跑测试确认没坏。"
    ),
    setup=_setup_rename,
    checks=[
        ("core.py 定义了 load_user", file_contains("core.py", "def load_user")),
        ("core.py 无残留", file_not_contains("core.py", "fetch_user")),
        ("api.py 无残留", file_not_contains("api.py", "fetch_user")),
        ("test_core.py 无残留", file_not_contains("test_core.py", "fetch_user")),
        ("测试仍然通过", tests_pass()),
        ("遵循编辑约定（用 str_replace 而不是 bash 改文件）", used_tool("str_replace")),
        ("没有反复试错", no_tool_errors(max_errors=2)),
    ],
)


CASES: list[Case] = [
    CASE_FIX_AND_TEST,
    CASE_TARGETED_EDIT,
    CASE_READONLY_ANSWER,
    CASE_MULTI_FILE_RENAME,
]


def by_name(name: str) -> Case | None:
    return next((case for case in CASES if case.name == name), None)
