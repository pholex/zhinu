"""装好的 wheel 黑盒冒烟：全新 venv 装 dist/*.whl，离开源码树跑一遍。

解决的问题：tests/ 里所有 e2e 都是 `python -m xiaoyu` 从源码树跑，随包文件
漏打包（package-data 没写全、`packages` 显式清单漏了新子包）在那里永远测不
出来——源码树里文件都在。只有"装出来的东西"才暴露这类问题，而它恰恰是用户
`pip install xiaoyu-agent` 拿到的东西。

不是 unittest 用例（文件名不以 test 开头，discover 不收）：它要一个构建好的
wheel 和能装依赖的网络，由 CI build job 在 `python -m build` 之后显式调起。
只依赖标准库，用宿主 python 驱动、venv 里的解释器被测。

    python tests/wheel_smoke.py [dist 目录或 .whl 路径，缺省 dist/]

检查项（任何一项不过都以非零退出，报错写明哪一步、期望什么、实际什么）：
1. 全新 venv 装 wheel（连同锁定依赖）；
2. 切到仓库外的临时目录，`xiaoyu.__file__` 必须落在该 venv 的 site-packages
   下——落回源码树等于什么都没测；
3. `xiaoyu.__version__` 与 importlib.metadata 版本都等于 wheel 文件名里的版本；
4. 源码树里 git 跟踪的 xiaoyu/ 下每个文件（模块 + 数据文件）装出来都在；
5. 运行期按包路径读取的数据文件逐个核对：docs/extending.md（system prompt
   引用）、evals/models.example.json（eval 数据兜底，且能读出候选）；
6. 四个 console script 都可调（`--version` / `--help`）；
7. keyless scripted 对话一轮（bash + write_file 两个工具调用），断言事件流、
   落盘文件、双向消费检查，以及 system prompt 里引用的扩展指南路径指向
   已安装的包。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST_NAME = "xiaoyu_agent"
_TIMEOUT = 120  # 单个子进程的硬上限（秒），pip 安装另给

#  运行期按包路径读取的数据文件（相对 site-packages），及读取方——
#  新增"按 __file__ 定位的数据文件"时同步加一行，并确认 pyproject 的
#  package-data 已覆盖
RUNTIME_DATA = {
    "xiaoyu/docs/extending.md": "agent.py 把它的绝对路径写进 system prompt",
    "xiaoyu/evals/models.example.json": "evals/models.py 找不到本机数据时的兜底",
}

#  scripted DSL：第一轮 bash + write_file 两个工具调用，第二轮收尾正文。
#  格式见 xiaoyu/scripted.py
SCRIPT = """\
tool_call: {"name": "bash", "arguments": {"command": "echo wheel-smoke-ok"}}
tool_call: {"name": "write_file", "arguments": {"path": "smoke.txt", "content": "installed-wheel"}}
usage: {"prompt_tokens": 100, "completion_tokens": 20}
---
text: 冒烟完成
usage: {"prompt_tokens": 150, "completion_tokens": 5}
"""


class SmokeFailure(Exception):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def ok(message: str) -> None:
    print(f"   [ok] {message}", flush=True)


def run(cmd: list[str], *, timeout: int = _TIMEOUT, **kwargs) -> subprocess.CompletedProcess:
    #  stdin 断开：headless 模式会把非 tty 的 stdin 当指令读（见 test_e2e_scripted）
    proc = subprocess.run(
        cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, **kwargs,
    )
    return proc


def describe(proc: subprocess.CompletedProcess) -> str:
    return (
        f"命令：{' '.join(map(str, proc.args))}\n退出码：{proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout[-4000:]}\n--- stderr ---\n{proc.stderr[-4000:]}"
    )


def find_wheel(target: Path) -> Path:
    if target.is_file():
        return target
    wheels = sorted(target.glob(f"{DIST_NAME}-*.whl"))
    check(len(wheels) == 1, f"{target} 下应恰好一个 {DIST_NAME} wheel，实际：{[w.name for w in wheels]}")
    return wheels[0]


def clean_env(**extra: str) -> dict[str, str]:
    """剔除会把源码树/本机状态带进来的变量，再叠加调用方要的。"""
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", "XIAOYU_EVAL_MODELS",
                "XIAOYU_SNAPSHOT_RECORD", "XIAOYU_SCRIPTED_CAPTURE", "XIAOYU_SCRIPTED_STRICT"):
        env.pop(key, None)
    env.update(extra)
    return env


def tracked_package_files() -> list[str] | None:
    """源码树里 git 跟踪的 xiaoyu/ 文件（posix 相对路径）；拿不到 git 返回 None。"""
    if not shutil.which("git"):
        return None
    proc = run(["git", "-C", str(REPO), "ls-files", "--", "xiaoyu"])
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return sorted(line.strip() for line in proc.stdout.splitlines() if line.strip())


def main(argv: list[str]) -> int:
    target = Path(argv[1]).resolve() if len(argv) > 1 else REPO / "dist"
    wheel = find_wheel(target)
    #  wheel 文件名：{distribution}-{version}-{python}-{abi}-{platform}.whl
    wheel_version = wheel.name.split("-")[1]
    print(f"wheel：{wheel}（版本 {wheel_version}）")

    with tempfile.TemporaryDirectory(prefix="xiaoyu-wheel-smoke-", ignore_cleanup_errors=True) as raw:
        tmp = Path(os.path.realpath(raw))
        check(REPO not in tmp.parents, f"临时目录 {tmp} 落在仓库内，隔离失效")
        venv = tmp / "venv"
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        exe = ".exe" if os.name == "nt" else ""
        py = bindir / f"python{exe}"
        outside = tmp / "outside"  # 仓库外的 cwd
        outside.mkdir()

        step("1. 全新 venv 安装 wheel")
        proc = run([sys.executable, "-m", "venv", str(venv)])
        check(proc.returncode == 0, f"创建 venv 失败\n{describe(proc)}")
        proc = run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q", str(wheel)],
                   timeout=900, cwd=outside, env=clean_env())
        check(proc.returncode == 0, f"pip 安装 wheel 失败\n{describe(proc)}")
        ok(f"已装进 {venv}")

        step("2-5. 导入位置 / 版本 / 随包文件")
        probe = r"""
import importlib.metadata, json, sys, sysconfig
import xiaoyu
from xiaoyu.evals import models
print(json.dumps({
    "file": xiaoyu.__file__,
    "version": xiaoyu.__version__,
    "dist_version": importlib.metadata.version("xiaoyu-agent"),
    "purelib": sysconfig.get_paths()["purelib"],
    "prefix": sys.prefix,
    "eval_data_path": str(models.data_path()),
    "eval_candidates": len(models.CANDIDATES),
}))
"""
        proc = run([str(py), "-c", probe], cwd=outside, env=clean_env())
        check(proc.returncode == 0, f"导入探针失败\n{describe(proc)}")
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        purelib = Path(info["purelib"]).resolve()
        pkg_dir = Path(info["file"]).resolve().parent
        check(Path(info["prefix"]).resolve() == venv.resolve(),
              f"探针跑在错误的解释器里：sys.prefix={info['prefix']}，期望 {venv}")
        check(pkg_dir == purelib / "xiaoyu",
              f"xiaoyu 导入自 {pkg_dir}，期望 venv 的 site-packages：{purelib / 'xiaoyu'}")
        check(REPO not in pkg_dir.parents, f"xiaoyu 从源码树导入（{pkg_dir}），没测到装出来的包")
        ok(f"xiaoyu.__file__ 在 {pkg_dir}")
        check(info["version"] == wheel_version,
              f"xiaoyu.__version__={info['version']} 与 wheel 文件名版本 {wheel_version} 不一致")
        check(info["dist_version"] == wheel_version,
              f"安装元数据版本 {info['dist_version']} 与 wheel 文件名版本 {wheel_version} 不一致")
        ok(f"__version__ 与元数据版本均为 {wheel_version}")

        tracked = tracked_package_files()
        if tracked is None:
            print("   [skip] 拿不到 git 跟踪清单，跳过全量随包文件核对（仍核对运行期数据文件）")
        else:
            missing = [rel for rel in tracked if not (purelib / rel).is_file()]
            check(not missing,
                  "源码树跟踪、但装出来缺失的文件（查 pyproject 的 packages / package-data）：\n  "
                  + "\n  ".join(missing))
            ok(f"git 跟踪的 {len(tracked)} 个 xiaoyu/ 文件装出来全都在")
        for rel, reader in RUNTIME_DATA.items():
            path = purelib / rel
            check(path.is_file() and path.stat().st_size > 0,
                  f"运行期数据文件缺失或为空：{path}（{reader}）")
            ok(f"{rel}（{reader}）")
        check(Path(info["eval_data_path"]).resolve() == (purelib / "xiaoyu/evals/models.example.json").resolve(),
              f"eval 数据未回落到随包示例：{info['eval_data_path']}")
        check(info["eval_candidates"] > 0, "随包 models.example.json 读不出任何候选模型")
        ok(f"evals 从随包示例读出 {info['eval_candidates']} 个候选")

        step("6. console scripts")
        for name in ("xiaoyu", "xy", "xiaoyu-agent"):
            script = bindir / f"{name}{exe}"
            check(script.is_file(), f"console script 未安装：{script}")
            proc = run([str(script), "--version"], cwd=outside, env=clean_env())
            check(proc.returncode == 0 and proc.stdout.strip() == f"xiaoyu {wheel_version}",
                  f"`{name} --version` 输出不符（期望 'xiaoyu {wheel_version}'）\n{describe(proc)}")
            ok(f"{name} --version → {proc.stdout.strip()}")
        script = bindir / f"xiaoyu-eval{exe}"
        check(script.is_file(), f"console script 未安装：{script}")
        proc = run([str(script), "--help"], cwd=outside, env=clean_env())
        check(proc.returncode == 0 and "usage" in proc.stdout.lower(),
              f"`xiaoyu-eval --help` 失败\n{describe(proc)}")
        ok("xiaoyu-eval --help")

        step("7. keyless scripted 对话（bash + write_file）")
        home, config, workspace, capture = (tmp / d for d in ("home", "config", "ws", "capture"))
        for d in (home, config, workspace):
            d.mkdir()
        script_path = tmp / "script.txt"
        script_path.write_text(SCRIPT, encoding="utf-8")
        #  隔离环境照抄 tests/test_e2e_scripted.py 的 env_for：配置/会话/技能全圈进
        #  临时目录，扩展面全关，沙箱关（断言的不是沙箱）
        env = clean_env(
            XIAOYU_SCRIPTED_SCRIPTS=str(script_path),
            XIAOYU_SCRIPTED_CAPTURE=str(capture),
            XIAOYU_SCRIPTED_STRICT="1",
            HOME=str(home), USERPROFILE=str(home),
            XDG_CONFIG_HOME=str(config), APPDATA=str(config),
            XIAOYU_ENABLE_SKILLS="0", XIAOYU_ENABLE_PLUGINS="0", XIAOYU_ENABLE_MCP="0",
            XIAOYU_ENABLE_HOOKS="0", XIAOYU_ENABLE_AGENTS="0", XIAOYU_ENABLE_EXPLORE="0",
            XIAOYU_ENABLE_WEB_SEARCH="0", XIAOYU_ENABLE_BROWSER="0",
            XIAOYU_SANDBOX="0",
        )
        proc = run(
            [str(bindir / f"xiaoyu{exe}"), "跑一遍冒烟", "--output-format", "stream-json",
             "--workspace", str(workspace), "--model", "smoke-model", "--yolo"],
            cwd=outside, env=env,
        )
        check(proc.returncode == 0, f"scripted 对话退出码非零\n{describe(proc)}")
        check("ScriptedUnconsumed" not in proc.stderr, f"脚本轮次没被消费完\n{describe(proc)}")
        try:
            events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise SmokeFailure(f"stream-json 输出不是合法 NDJSON：{exc}\n{describe(proc)}") from exc
        completed = {e["name"]: e for e in events if e.get("kind") == "tool.completed"}
        check(set(completed) == {"bash", "write_file"},
              f"期望 bash 与 write_file 各完成一次，实际：{sorted(completed)}\n{describe(proc)}")
        check(all(e.get("ok") for e in completed.values()),
              f"有工具调用失败：{json.dumps(completed, ensure_ascii=False)}")
        check("wheel-smoke-ok" in completed["bash"].get("output", ""),
              f"bash 输出不含 wheel-smoke-ok：{completed['bash']}")
        ok("bash 工具输出 wheel-smoke-ok")
        written = workspace / "smoke.txt"
        check(written.is_file() and written.read_text(encoding="utf-8") == "installed-wheel",
              f"write_file 没把文件落到工作区：{written}")
        ok("write_file 落盘 smoke.txt")
        results = [e for e in events if e.get("kind") == "result"]
        check(len(results) == 1 and results[0].get("result") == "冒烟完成",
              f"result 收尾事件不符：{results}")
        check(results[0].get("usage", {}).get("turns") == 2, f"应恰好两次模型调用：{results[0]}")
        ok("result = 冒烟完成（2 次模型调用）")
        requests = sorted(capture.glob("request-*.json"))
        check(requests, "scripted 桩没有捕获到请求头")
        system = json.loads(requests[0].read_text(encoding="utf-8")).get("system", "")
        extending = str(purelib / "xiaoyu" / "docs" / "extending.md")
        check(extending in system,
              f"system prompt 没引用已安装包里的扩展指南（期望路径 {extending}）")
        ok("system prompt 引用的扩展指南指向已安装包")

    print("\n冒烟通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SmokeFailure as exc:
        print(f"\n::error::wheel 冒烟失败\n{exc}", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired as exc:
        print(f"\n::error::wheel 冒烟失败：子进程超时（{exc.timeout}s）：{exc.cmd}", file=sys.stderr)
        sys.exit(1)
