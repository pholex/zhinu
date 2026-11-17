#!/usr/bin/env python3
"""实测每个内置型号收不收得下图片输入，产出可直接粘进 PRESETS 的 vision_models。

`providers.PRESETS` 的纪律是**只写确认过的事实**（模型名猜错=404，协议猜错=每轮
400），视觉能力同理且更隐蔽：声明成"能看"而实际不能，症状是工具一回图整个会话
就卡在 400 上，而错误信息通常只说 "invalid content type"，指不回这张声明表。
所以这份数据必须实测，不能按厂商文档或直觉填。

用法（配好 .env 或环境变量里的各家 key）：

    .venv/bin/python experiments/vision_probe.py            # 全部已配置的直连
    .venv/bin/python experiments/vision_probe.py deepseek   # 只探一家

每个型号发一次最小请求：一张 320×320 四象限色块图（左上绿/右上紫/左下蓝/右下橙）
+ 一句"四个象限各是什么颜色"，不带工具。**四种颜色全部答对才算通过。**
成本约等于零，但确实会花钱、走真实网络。协议按 providers 已声明的
responses_models / anthropic_models 走，与线上一致。

判法沿革：第一版是绿、紫两张纯色图各发一轮、两轮全中（蒙对概率才低到可下结论）；
2026-08-24 起升级为单图四象限——四色全中的蒙中概率比两轮纯色又低几个量级，
且同时验证了模型确实在解析像素位置（deepseek-v4-flash-vision-exp 入册时先以
xiaoyu 全管线端到端跑通了这个判法，再沉淀回本脚本）。请求数还省了一半。

判据的几条纪律都是踩出来的（细节见下面各处 ⚠️，别凭直觉改）：不看 HTTP 状态码、
不给"看不到就明说"的逃生舱、图不能太小、每次带 nonce 打散网关缓存、
异常重试一次但答错不重试。
"""

from __future__ import annotations

import base64
import os
import secrets
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoyu import providers  # noqa: E402
from xiaoyu.config import Config, load_dotenv  # noqa: E402


#  ⚠️ 图别做太小。第一版用 8×8，xai 直接 400：
#  "Image has 64 total pixels (8x8), which is below the minimum of 512 pixels"，
#  qwen 也回"image length and width do not meet the model restrictions"——
#  这两条**恰恰证明它们收下并解析了图片部件**，却会被当成"不支持"记进表里。
#  320×320 时每个象限 160×160，远超各家下限；纯色块压缩后仍只有几百字节。
_SIDE = 320

#  ⚠️ **刻意不用红色**。"这张图是什么颜色"在没看到图时最容易蒙的答案就是红——
#  四个颜色里混进红，等于给"200 收下却静默丢图"的端点送一格免费命中。
#  判法要求下面四色**全部**出现在回答里：蒙中概率低到可以忽略，
#  而真丢了图的端点一个色名都说不出来。
_QUADRANTS: tuple[tuple[str, tuple[int, int, int], tuple[str, ...]], ...] = (
    ("左上", (0, 190, 0), ("绿", "green")),
    ("右上", (150, 0, 180), ("紫", "purple", "violet")),
    ("左下", (0, 80, 220), ("蓝", "blue")),
    ("右下", (240, 140, 0), ("橙", "橘", "orange")),
)


def _quad_png() -> bytes:
    """320×320 四象限色块 PNG，手搓（不引 PIL：这个脚本不该有依赖）。"""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return len(payload).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")

    half = _SIDE // 2
    top_left, top_right = _QUADRANTS[0][1], _QUADRANTS[1][1]
    bottom_left, bottom_right = _QUADRANTS[2][1], _QUADRANTS[3][1]
    rows = []
    for y in range(_SIDE):
        left, right = (top_left, top_right) if y < half else (bottom_left, bottom_right)
        rows.append(b"\x00" + bytes(left) * half + bytes(right) * half)
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", _SIDE, _SIDE, 8, 2, 0, 0, 0))
    return (
        b"\x89PNG\r\n\x1a\n"
        + header
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


#  ⚠️⚠️ **绝对不要给模型"看不到就明说"的逃生舱**。这半句看着是严谨（想抓"200
#  收下却静默丢图"的端点），实际是本脚本踩过最深的坑：加上
#  "如果你看不到图片，就回答：看不到图" 之后，claude-opus-5 / claude-sonnet-5
#  **100% 回答"看不到图"，尽管它们看得一清二楚**——同一张图问"描述你看到的
#  图片"，两个型号都能准确说出"纯蓝 #0000FF""紫色"。逐变量隔离过：与 max_tokens
#  无关（200/4096 都复现），就是这半句造成的假阴性，第一版据此把 anthropic 错记成
#  "官方兼容层丢图"，还顺手写了一段"纯 OpenAI 兼容第一次露出实价"的错误结论。
#  正确判据是**答不出颜色即失败**，不需要模型自证清白：真丢了图的端点说不出
#  这四种颜色，而能看见的模型不会被一句提示词说服自己是瞎的。
#
#  顺序（左上→右下）写进问题是引导模型逐格看图，但**刻意不进判据**：各家措辞
#  太多样（"依次是""左上角为"……），按序匹配全是解析坑；四色齐全已经零蒙中。
QUESTION = "这张图分为四个象限，请按 左上/右上/左下/右下 的顺序，各用一个词说出颜色。"

#  ⚠️ 每次请求带一个一次性 nonce **打散网关的响应缓存**。踩过：拿用户自建的
#  LiteLLM 网关探 `gateway/claude-opus-5` 时，同一张纯蓝图连续 6 次返回一字不差
#  的同一句话，而同轮的绿图、黄图正常——那不是模型时好时坏，是第一次的响应
#  被缓存粘住了。**连续几次输出完全逐字一致，就是缓存最好认的指纹。**
#  探直连厂商时这行是白搭（各家不缓存输出），但探网关路由时它是结论有效的前提，
#  所以无条件加：多几个 token 的代价，换掉"测出来的是缓存不是模型"这类假结论。
_NONCE_HINT = "（本次编号 {nonce}，无需理会）"

#  ⚠️ 别把 max_tokens 卡太死。第一版给 30，推理型号（claude-opus-5 / kimi-k3 /
#  deepseek-v4-flash）把额度全花在推理上、正文空着回来，会被误记成"不支持"；
#  512 对 deepseek-v4-flash 仍不够（completion_tokens 打满 512 仍无正文）。
MAX_TOKENS = 4096


def _ask(route) -> str:
    data_url = "data:image/png;base64," + base64.b64encode(_quad_png()).decode()
    question = QUESTION + _NONCE_HINT.format(nonce=secrets.token_hex(4))
    response = route.client.chat.completions.create(
        model=route.model,
        max_tokens=MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    )
    return (response.choices[0].message.content or "").strip()


def probe(registry: providers.Registry, model: str) -> tuple[bool, str]:
    """四个象限的颜色全部答对才算真看得见。报错重试一次仍报错/漏色即判否。

    ⚠️ **只有异常才重试，答错不重试**。重试的由头：2026-08-24 全量跑时
    qwen3.8-max 回了一次瞬时 400（InternalError.Algo.InvalidParameter，
    复测 8/8 全过）——探测结论要进长期声明表，一次服务端抖动不该把"能看"
    记成"不收图"。真不支持的端点重试也是同一句 400，多花的只有失败路径上
    一个请求。答错（漏色）不给第二次机会：那是判据本体，重试等于给蒙中开门。"""
    route = registry.resolve(model)
    text = ""
    for attempt in (1, 2):
        try:
            text = _ask(route)
            break
        except Exception as exc:  # noqa: BLE001 - 探测脚本，任何失败都是结论的一部分
            if attempt == 2:
                return False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:150]}"
    lowered = text.lower()
    missing = [
        names[0]
        for _pos, _rgb, names in _QUADRANTS
        if not any(name in lowered for name in names)
    ]
    shown = text[:60].replace("\n", " ") or "(空回复)"
    if missing:
        return False, f"缺 {'/'.join(missing)}：{shown}"
    return True, shown


def main(argv: list[str]) -> int:
    wanted = set(argv[1:])
    #  key 通常在 .env 里（CLI 每条路径都先 load_dotenv，脚本得自己来）
    load_dotenv()
    registry = providers.build(Config.from_env())
    configured = {provider.name for provider in registry.providers}
    results: dict[str, list[str]] = {}
    for name, preset in providers.PRESETS.items():
        if name not in configured or (wanted and name not in wanted):
            continue
        for model in preset.models:
            ok, detail = probe(registry, model)
            protocol = "responses" if registry.client(name).speaks_responses(model) else "chat"
            print(f"{'✅' if ok else '❌'} {model:<18} [{protocol}] {detail}", flush=True)
            if ok:
                results.setdefault(name, []).append(model)

    if not results:
        print("\n没有型号通过——没配 key，或者这批型号都不收图。")
        return 1
    print("\n粘进 xiaoyu/providers.py 的对应 Preset：")
    for name, models in results.items():
        full = list(providers.PRESETS[name].models)
        value = '(WILDCARD,)' if models == full else "(" + ", ".join(f'"{m}"' for m in models) + ",)"
        print(f'  {name}: vision_models={value}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
