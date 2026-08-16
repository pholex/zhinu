#!/usr/bin/env python3
"""实测每个内置型号收不收得下图片输入，产出可直接粘进 PRESETS 的 vision_models。

`providers.PRESETS` 的纪律是**只写确认过的事实**（模型名猜错=404，协议猜错=每轮
400），视觉能力同理且更隐蔽：声明成"能看"而实际不能，症状是工具一回图整个会话
就卡在 400 上，而错误信息通常只说 "invalid content type"，指不回这张声明表。
所以这份数据必须实测，不能按厂商文档或直觉填。

用法（配好 .env 或环境变量里的各家 key）：

    .venv/bin/python experiments/vision_probe.py            # 全部已配置的直连
    .venv/bin/python experiments/vision_probe.py deepseek   # 只探一家

每个型号发两次最小请求：一张 64×64 纯色 PNG（绿、紫各一轮）+ 一句"这张图是
什么颜色"，不带工具。**两轮都答对才算通过。** 成本约等于零，但确实会花钱、
走真实网络。两种协议各按 providers 已声明的 responses_models 走，与线上一致。

判据的四条纪律都是踩出来的（细节见下面各处 ⚠️，别凭直觉改）：不看 HTTP 状态码、
不给"看不到就明说"的逃生舱、图不能小于 512 像素、每次带 nonce 打散网关缓存。
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoyu import providers  # noqa: E402
from xiaoyu.config import Config, load_dotenv  # noqa: E402


#  ⚠️ 图别做太小。第一版用 8×8，xai 直接 400：
#  "Image has 64 total pixels (8x8), which is below the minimum of 512 pixels"，
#  qwen 也回"image length and width do not meet the model restrictions"——
#  这两条**恰恰证明它们收下并解析了图片部件**，却会被当成"不支持"记进表里。
#  64×64 = 4096 像素，过了各家下限，仍只有几百字节。
_SIDE = 64


def _solid_png(rgb: tuple[int, int, int]) -> bytes:
    """64×64 纯色 PNG，手搓（不引 PIL：这个脚本不该有依赖）。"""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return len(payload).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")

    size = _SIDE.to_bytes(4, "big")
    header = chunk(b"IHDR", size + size + bytes([8, 2, 0, 0, 0]))
    raw = b"".join(b"\x00" + bytes(rgb) * _SIDE for _ in range(_SIDE))
    return b"\x89PNG\r\n\x1a\n" + header + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


#  ⚠️ **必须两种颜色都答对才算通过，且刻意不用红色**。"这张图是什么颜色"在没
#  看到图时最容易蒙的答案就是红——只发红图时蒙对一次就能把一家错记成支持。
#  绿 + 紫两轮全中，蒙对的概率才低到可以下结论。
CASES = (("绿", (0, 200, 0)), ("紫", (128, 0, 128)))

#  ⚠️⚠️ **绝对不要给模型"看不到就明说"的逃生舱**。这半句看着是严谨（想抓"200
#  收下却静默丢图"的端点），实际是本脚本踩过最深的坑：加上
#  "如果你看不到图片，就回答：看不到图" 之后，claude-opus-5 / claude-sonnet-5
#  **100% 回答"看不到图"，尽管它们看得一清二楚**——同一张图问"描述你看到的
#  图片"，两个型号都能准确说出"纯蓝 #0000FF""紫色"。逐变量隔离过：与 max_tokens
#  无关（200/4096 都复现），就是这半句造成的假阴性，第一版据此把 anthropic 错记成
#  "官方兼容层丢图"，还顺手写了一段"纯 OpenAI 兼容第一次露出实价"的错误结论。
#  正确判据是**答不出颜色即失败**，不需要模型自证清白：真丢了图的端点说不出
#  "绿"和"紫"，而能看见的模型不会被一句提示词说服自己是瞎的。
QUESTION = "这张图是什么颜色？只回答颜色。"

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


def _ask(route, rgb: tuple[int, int, int]) -> str:
    data_url = "data:image/png;base64," + base64.b64encode(_solid_png(rgb)).decode()
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
    """两轮不同颜色都答对才算真看得见。任一轮报错/答错即判否。"""
    route = registry.resolve(model)
    answers: list[str] = []
    for expected, rgb in CASES:
        try:
            text = _ask(route, rgb)
        except Exception as exc:  # noqa: BLE001 - 探测脚本，任何失败都是结论的一部分
            return False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:150]}"
        answers.append(text[:20] or "(空回复)")
        if expected not in text:
            return False, " / ".join(answers)
    return True, " / ".join(answers)


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
