"""多模态内核链路：内容部件、落盘缓存、出网展开、能力闸门。不打网络。

这条链路的失败形态几乎全是**静默错值**（图片被 str() 成一坨字典喂给 hook、
按字符估 token 把一张图算成免费、引用没展开就发出去），所以每一处收窄
content 的地方都要有用例钉住。
"""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import compaction, media, providers, responses, session_log, tokens

PNG = b"\x89PNG\r\n\x1a\n fake bytes"


def parts_message(text: str = "看这张图") -> dict:
    ref = media.store(PNG, "image/png")
    return {"role": "user", "content": [media.text_part(text), media.image_part(ref)]}


class PartsTest(unittest.TestCase):
    def test_text_of_handles_every_shape(self):
        self.assertEqual(media.text_of(None), "")
        self.assertEqual(media.text_of("裸字符串"), "裸字符串")
        self.assertEqual(
            media.text_of([media.text_part("a"), media.image_part("x"), media.text_part("b")]),
            "a[图片]b",
        )

    def test_text_of_never_leaks_raw_dict(self):
        """回归钉子：旧写法 str(content) 会把部件列表原样喂给 hook / 会话预览。"""
        text = media.text_of(parts_message()["content"])
        self.assertNotIn("image_url", text)
        self.assertNotIn("{", text)

    def test_as_parts_round_trip(self):
        self.assertEqual(media.as_parts("x"), [media.text_part("x")])
        self.assertEqual(media.as_parts(None), [])
        parts = [media.text_part("a")]
        self.assertEqual(media.as_parts(parts), parts)


class CacheTest(unittest.TestCase):
    def test_content_addressed_and_deduped(self):
        first = media.store(PNG, "image/png")
        second = media.store(PNG, "image/png")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(media.SCHEME))
        self.assertTrue(first.endswith(".png"))

    def test_store_base64_rejects_garbage(self):
        ref, problem = media.store_base64("不是base64!!", "image/png")
        self.assertEqual(ref, "")
        self.assertIn("base64", problem)

    def test_store_base64_goes_through_accept(self):
        """MCP 图片入口必须走 accept 咽喉：体积超限与假 mime 都要被拒。

        超限图一旦以引用进了历史，每轮请求都会重放它，上游拒一次=轮轮被拒。
        """
        #  合法 PNG 走通（自报 mime 错了也不影响——嗅探为准）
        ref, problem = media.store_base64(base64.b64encode(PNG).decode(), "image/jpeg")
        self.assertTrue(ref.startswith(media.SCHEME), problem)
        self.assertTrue(ref.endswith(".png"), "扩展名该来自魔数嗅探，不是自报 mime")
        #  超限：伪造一个 header 合法但体积超上限的 PNG
        big = PNG + b"\x00" * (media.MAX_IMAGE_BYTES + 1)
        ref, problem = media.store_base64(base64.b64encode(big).decode(), "image/png")
        self.assertEqual(ref, "")
        self.assertIn("上限", problem)
        #  非图片字节冒充 image/png：拒收并说明
        ref, problem = media.store_base64(base64.b64encode(b"just text").decode(), "image/png")
        self.assertEqual(ref, "")
        self.assertIn("格式", problem)

    def test_data_url_round_trip(self):
        ref = media.store(PNG, "image/png")
        url = media.data_url(ref)
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), PNG)

    def test_foreign_url_passes_through(self):
        self.assertEqual(media.data_url("https://x/y.png"), "https://x/y.png")

    def test_traversal_reference_refused(self):
        """引用可能来自会话文件/宿主：拼路径前必须校验形状。"""
        self.assertIsNone(media.path_of(media.SCHEME + "../../../etc/passwd"))
        self.assertIsNone(media.path_of(media.SCHEME + "nothex.png"))

    def test_missing_file_does_not_raise(self):
        ref = media.SCHEME + "0" * 64 + ".png"
        self.assertEqual(media.data_url(ref), ref)

    def test_write_failure_returns_empty(self):
        with mock.patch.object(Path, "mkdir", side_effect=OSError("只读盘")):
            self.assertEqual(media.store(b"whatever-unique-bytes", "image/png"), "")


class AcceptTest(unittest.TestCase):
    """进内核的每一张图都过 accept()：格式与体积只判一遍。"""

    def test_accepts_real_png(self):
        ref, problem = media.accept(PNG)
        self.assertTrue(ref.startswith(media.SCHEME))
        self.assertEqual(problem, "")

    def test_rejects_by_magic_not_suffix(self):
        """不信扩展名：jpg 存成 .png 是常事，猜错了上游只回一句语焉不详的错。"""
        self.assertEqual(media.sniff_mime(b"\xff\xd8\xff\xe0 jpeg"), "image/jpeg")
        self.assertEqual(media.sniff_mime(b"RIFF" + b"1234" + b"WEBP"), "image/webp")
        self.assertEqual(media.sniff_mime(b"<html>"), "")
        ref, problem = media.accept(b"<html>", "剪贴板里的内容")
        self.assertEqual(ref, "")
        self.assertIn("不是能识别的图片格式", problem)

    def test_rejects_oversize_with_actionable_reason(self):
        ref, problem = media.accept(PNG + b"x" * media.MAX_IMAGE_BYTES)
        self.assertEqual(ref, "")
        self.assertIn("MB", problem)

    def test_empty_input(self):
        self.assertEqual(media.accept(b"")[0], "")

    def test_accept_file_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            path.write_bytes(PNG)
            ref, problem = media.accept_file(path)
            self.assertEqual(problem, "")
            self.assertTrue(media.data_url(ref).startswith("data:image/png;base64,"))

    def test_accept_file_missing(self):
        ref, problem = media.accept_file(Path("/nope/missing.png"))
        self.assertEqual(ref, "")
        self.assertIn("missing.png", problem)


class SplitPathsTest(unittest.TestCase):
    """拖文件进终端 / @ 补全出来的路径。判据刻意收紧：宁可漏判不可误判。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / "shot.png"
        self.image.write_bytes(PNG)

    def test_single_path(self):
        self.assertEqual(media.split_paths(str(self.image)), [self.image])

    def test_multiple_paths(self):
        other = self.root / "b.png"
        other.write_bytes(PNG)
        self.assertEqual(
            media.split_paths(f"{self.image} {other}"), [self.image, other]
        )

    def test_quoted_and_escaped_forms(self):
        spaced = self.root / "my shot.png"
        spaced.write_bytes(PNG)
        #  加引号：两个平台的终端都会这么产出
        self.assertEqual(media.split_paths(f'"{spaced}"'), [spaced])
        #  `\ ` 转义空格是 POSIX 终端的形态。Windows 上反斜杠已经是路径分隔符，
        #  `my\ shot.png` 本身有歧义，不支持也不该支持
        if os.name != "nt":
            self.assertEqual(media.split_paths(str(spaced).replace(" ", "\\ ")), [spaced])

    def test_windows_backslash_paths_survive_splitting(self):
        """Windows 路径的 `\\` 是分隔符不是转义符。

        这条在 macOS/Linux 上**也真跑**：_shell_split 只按 os.name 分支，而
        shlex 本身跨平台行为一致，patch 掉 os.name 就能在本机验证 Windows 那条
        分支。v0.29.0/0.29.1 的教训是别把平台差异全押给 Windows CI——POSIX 模式
        的 shlex 把 C:\\Users\\me 吃成 C:Usersme，Windows 上整个拖文件功能是死的，
        本机却怎么跑都绿。
        """
        win = r"C:\Users\me\AppData\shot.png"
        with mock.patch.object(media.os, "name", "nt"):
            self.assertEqual(media._shell_split(win), [win])
            self.assertEqual(media._shell_split(f'"{win}"'), [win])
            self.assertEqual(media._shell_split(f"{win} {win}"), [win, win])
        #  两个分支都得显式 patch，别靠"当前跑在哪个平台"来选——本条最初就是
        #  这么写坏的：POSIX 那句裸跑，在 Windows CI 上走进 nt 分支后
        #  `/tmp/my\ shot.png` 被切成两段而红。
        with mock.patch.object(media.os, "name", "posix"):
            self.assertEqual(media._shell_split(r"/tmp/my\ shot.png"), ["/tmp/my shot.png"])

    def test_file_url(self):
        self.assertEqual(media.split_paths(self.image.as_uri()), [self.image])

    def test_file_url_with_spaces(self):
        """`file://` 里空格是 %20，还原时必须 unquote 回来。"""
        spaced = self.root / "my shot.png"
        spaced.write_bytes(PNG)
        self.assertIn("%20", spaced.as_uri())
        self.assertEqual(media.split_paths(spaced.as_uri()), [spaced])

    def test_file_uri_round_trips_on_this_platform(self):
        """as_uri() 出去、_path_from_file_uri 回来，必须还原成同一个路径。

        ⚠️ 这条**只有在 Windows 上跑才拦得住**它针对的 bug：Windows 的
        as_uri() 是 `file:///C:/…`，旧写法 `unquote(uri[7:])` 还原成 `/C:/…`
        这个不存在的路径；POSIX 下多切的那个 `/` 恰好就是根斜杠，所以
        macOS/Linux 永远绿。v0.29.0 就是这么发出去的——CI 里 Windows 两个
        Python 版本都红着，另外两个平台全绿。别因为本机绿就当它没在保护什么。
        """
        self.assertEqual(Path(media._path_from_file_uri(self.image.as_uri())), self.image)


class PathsFromUriListTest(unittest.TestCase):
    """剪贴板文件清单（Linux 的 text/uri-list、Windows 的 CF_HDROP）。

    这条路原先零覆盖，于是它和 split_paths 犯的是同一个 `file://` 裸切 bug、
    却连 Windows CI 都没红过——没有测试的分支不会报警，只会在用户那里静默失效。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / "shot.png"
        self.image.write_bytes(PNG)

    def test_uri_list_round_trips(self):
        self.assertEqual(media._paths_from_uri_list(self.image.as_uri()), (self.image,))

    def test_comments_blanks_and_missing_are_dropped(self):
        spaced = self.root / "my shot.png"
        spaced.write_bytes(PNG)
        raw = "\n".join(
            ["# comment", "", self.image.as_uri(), spaced.as_uri(), (self.root / "nope.png").as_uri()]
        )
        self.assertEqual(media._paths_from_uri_list(raw), (self.image, spaced))

    def test_mixed_kinds_all_returned(self):
        """图片与非图片混在一起照样返回：由调用方决定谁进 chip、谁留路径。"""
        doc = self.root / "note.pdf"
        doc.write_text("x")
        self.assertEqual(media.split_paths(f"{self.image} {doc}"), [self.image, doc])
        self.assertTrue(media.is_image_path(self.image))
        self.assertFalse(media.is_image_path(doc))

    def test_refuses_prose_and_missing(self):
        #  一段想让模型看的文字里恰好提到路径 —— 不能被吃成附件
        self.assertEqual(media.split_paths(f"看下 {self.image} 这张图"), [])
        #  一真一假：整体判否，绝不"能认几个算几个"
        self.assertEqual(media.split_paths(f"{self.image} {self.root / 'nope.png'}"), [])
        self.assertEqual(media.split_paths("多行\n路径.png"), [])
        self.assertEqual(media.split_paths(""), [])


class DimensionsTest(unittest.TestCase):
    """自己解四种格式的头，不引 PIL。解错的代价只是标签难看，所以认不出就退回体积。"""

    @staticmethod
    def png(width: int, height: int) -> bytes:
        head = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
        return head + width.to_bytes(4, "big") + height.to_bytes(4, "big")

    def test_png(self):
        self.assertEqual(media.dimensions(self.png(1920, 1080)), (1920, 1080))

    def test_gif(self):
        data = b"GIF89a" + (320).to_bytes(2, "little") + (240).to_bytes(2, "little")
        self.assertEqual(media.dimensions(data), (320, 240))

    def test_jpeg_walks_segment_chain(self):
        #  JPEG 没有定偏移，要顺着段链走到 SOF0
        data = (
            b"\xff\xd8"
            + b"\xff\xe0" + (16).to_bytes(2, "big") + b"x" * 14  # APP0，应被跳过
            + b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
            + (600).to_bytes(2, "big") + (800).to_bytes(2, "big")
        )
        self.assertEqual(media.dimensions(data), (800, 600))

    def test_webp_lossy(self):
        data = b"RIFF" + b"0000" + b"WEBP" + b"VP8 " + b"0" * 10
        data += (640).to_bytes(2, "little") + (480).to_bytes(2, "little")
        self.assertEqual(media.dimensions(data), (640, 480))

    def test_unknown_returns_none(self):
        self.assertIsNone(media.dimensions(b"nope"))
        self.assertIsNone(media.dimensions(b""))

    def test_label_falls_back_to_size_only(self):
        ref = media.store(PNG, "image/png")
        #  测试用的 PNG 是伪造的头，认不出尺寸 → 只报体积
        self.assertTrue(media.label(ref).endswith("KB"))
        real = media.store(self.png(64, 64) + b"padding", "image/png")
        self.assertTrue(media.label(real).startswith("64×64 · "))


class ClipboardTest(unittest.TestCase):
    def test_macos_parses_osascript_hex(self):
        payload = "«data PNGf" + PNG.hex().upper() + "»"
        proc = mock.Mock(returncode=0, stdout=payload)
        with mock.patch.object(media.sys, "platform", "darwin"), mock.patch.object(
            media, "_run", return_value=proc
        ):
            clip = media.clipboard()
        self.assertEqual(clip.images, (PNG,))
        self.assertEqual(clip.problem, "")

    def test_macos_multi_file_uses_as_list(self):
        """⚠️ 多文件时 `as «class furl»` 会 -1700 报错，只有 as list 撑得住。"""
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "a.png", Path(tmp) / "b.png"
            first.write_bytes(PNG)
            second.write_bytes(PNG)

            def fake_run(command, text=False):
                script = command[-1]
                if "PNGf" in script:
                    return mock.Mock(returncode=1, stdout="")
                return mock.Mock(returncode=0, stdout=f"{first}\n{second}\n")

            with mock.patch.object(media.sys, "platform", "darwin"), mock.patch.object(
                media, "_run", side_effect=fake_run
            ):
                clip = media.clipboard()
        self.assertEqual(clip.files, (first, second))
        self.assertEqual(clip.problem, "")

    def test_empty_clipboard_says_why(self):
        """取不到图必须给出可行动的原因——"按了没反应"最难自查。"""
        proc = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(media.sys, "platform", "darwin"), mock.patch.object(
            media, "_run", return_value=proc
        ):
            clip = media.clipboard()
        self.assertEqual(clip.images, ())
        self.assertIn("剪贴板", clip.problem)

    def test_linux_without_tools_names_the_tool(self):
        with mock.patch.object(media.sys, "platform", "linux"), mock.patch.object(
            media.shutil, "which", return_value=None
        ):
            clip = media.clipboard()
        self.assertIn("wl-paste", clip.problem)
        self.assertIn("xclip", clip.problem)


class InlineTest(unittest.TestCase):
    def test_expands_reference_at_the_wire(self):
        messages = [{"role": "system", "content": "s"}, parts_message()]
        out = media.inline(messages)
        url = out[1]["content"][1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        #  历史本身不能被就地改写：内核里存的仍是引用
        self.assertTrue(messages[1]["content"][1]["image_url"]["url"].startswith(media.SCHEME))

    def test_textonly_history_not_copied(self):
        messages = [{"role": "user", "content": "纯文本"}]
        self.assertIs(media.inline(messages), messages)


class TokenTest(unittest.TestCase):
    def test_image_not_estimated_by_characters(self):
        """引用只有 ~80 字符，但一张图真实要花上千 token——照字面估压缩永不触发。"""
        message = parts_message("")
        by_chars = tokens.estimate_text(message["content"][1]["image_url"]["url"])
        self.assertGreater(tokens.estimate_content(message["content"]), by_chars * 5)
        self.assertGreaterEqual(tokens.estimate_content(message["content"]), media.IMAGE_TOKENS)

    def test_plain_text_unchanged(self):
        self.assertEqual(tokens.estimate_content("abc"), tokens.estimate_text("abc"))


class CompactionTest(unittest.TestCase):
    def test_user_voice_backup_reads_text_not_dict(self):
        picked = compaction.collect_user_voice([parts_message("原话在此")])
        self.assertIn("原话在此", picked)
        self.assertNotIn("image_url", picked)

    def test_merge_consecutive_users_keeps_image(self):
        merged = compaction.merge_consecutive_users(
            [{"role": "user", "content": "前一条"}, parts_message("后一条")]
        )
        self.assertEqual(len(merged), 1)
        content = merged[0]["content"]
        self.assertEqual(len(media.images_of(content)), 1, "拼接不能把图片拼没了")
        self.assertIn("前一条", media.text_of(content))
        self.assertIn("后一条", media.text_of(content))

    def test_render_transcript_has_no_raw_parts(self):
        self.assertNotIn("image_url", compaction.render([parts_message()]))


class SessionLogTest(unittest.TestCase):
    def test_preview_is_readable(self):
        text = media.text_of(parts_message("截图看看")["content"])
        self.assertTrue(text.startswith("截图看看"))

    def test_turn_starts_counts_image_message(self):
        starts = session_log.turn_starts([{"role": "system", "content": "s"}, parts_message()])
        self.assertEqual(starts, [1])


class ResponsesTest(unittest.TestCase):
    def test_parts_translated_to_input_image(self):
        items = responses.to_input([parts_message("看图")])
        content = items[0]["content"]
        self.assertEqual(content[0], {"type": "input_text", "text": "看图"})
        self.assertEqual(content[1]["type"], "input_image")
        self.assertTrue(content[1]["image_url"].startswith(media.SCHEME))

    def test_assistant_text_uses_output_text(self):
        items = responses.to_input(
            [{"role": "assistant", "content": [media.text_part("答")]}]
        )
        self.assertEqual(items[0]["content"][0]["type"], "output_text")

    def test_transport_inlines_on_all_protocols(self):
        """出网前必须展开成真实字节，三条协议一致（anthropic 侧是 base64 source）。"""
        for protocol in ("chat", "responses", "anthropic"):
            with self.subTest(protocol=protocol):
                inner = mock.MagicMock()
                anthro = mock.MagicMock()
                transport = responses.wrap(
                    inner,
                    (responses.WILDCARD,) if protocol == "responses" else (),
                    (responses.WILDCARD,) if protocol == "anthropic" else (),
                    lambda: anthro,
                )
                transport.chat.completions.create(model="m", messages=[parts_message()])
                if protocol == "responses":
                    sent = inner.responses.create.call_args.kwargs["input"]
                    url = sent[0]["content"][1]["image_url"]
                    self.assertTrue(url.startswith("data:"), "出网前必须展开成 data URL")
                elif protocol == "anthropic":
                    sent = anthro.messages.create.call_args.kwargs["messages"]
                    source = sent[0]["content"][1]["source"]
                    self.assertEqual(source["type"], "base64")
                    self.assertEqual(source["media_type"], "image/png")
                    self.assertEqual(source["data"], base64.b64encode(PNG).decode())
                else:
                    sent = inner.chat.completions.create.call_args.kwargs["messages"]
                    url = sent[0]["content"][1]["image_url"]["url"]
                    self.assertTrue(url.startswith("data:"), "出网前必须展开成 data URL")


class VisionGateTest(unittest.TestCase):
    def registry(self, vision: tuple[str, ...] = ()) -> providers.Registry:
        return providers.Registry(
            [
                providers.Provider("direct", "u", "k", ("m-see", "m-blind"), "直连", (), vision),
                providers.Provider("gateway", "u", "k", (), "网关"),
            ],
            clients={"direct": mock.MagicMock(), "gateway": mock.MagicMock()},
        )

    def test_declared_per_model_not_per_vendor(self):
        registry = self.registry(("m-see",))
        self.assertTrue(registry.sees_images("m-see"))
        self.assertFalse(registry.sees_images("m-blind"))

    def test_fail_closed_for_wildcard_and_unknown(self):
        registry = self.registry()
        #  网关后面挂的是什么模型无从知道，猜"能"等于每轮 400
        self.assertFalse(registry.sees_images("随便什么名字"))
        self.assertFalse(registry.sees_images("m-see"))

    def test_env_override_opens_named_models(self):
        registry = self.registry()
        with mock.patch.dict("os.environ", {"XIAOYU_VISION_MODELS": "网关上的视觉模型"}):
            self.assertTrue(registry.sees_images("网关上的视觉模型"))
            self.assertFalse(registry.sees_images("别的"))

    def test_pinned_name_resolves_to_its_provider(self):
        registry = self.registry(("m-see",))
        self.assertTrue(registry.sees_images("direct/m-see"))
        self.assertFalse(registry.sees_images("gateway/m-see"))


class PresetDeclarationTest(unittest.TestCase):
    """按型号声明的两张表（协议 / 视觉）不能出现拼错的型号名。

    拼错不会报错，只会**静默失效**：`vision_models=("gpt-5.6-sun",)` 的效果
    与不写完全一样，而症状是线上图片被降级成一行文字说明——没人会想到回来
    查这张表。同理拼错 responses_models 会悄悄退回 chat 协议。
    """

    def test_declarations_reference_real_models(self):
        for name, preset in providers.PRESETS.items():
            for field in ("responses_models", "vision_models"):
                for declared in getattr(preset, field):
                    with self.subTest(preset=name, field=field, model=declared):
                        if declared == providers.WILDCARD:
                            continue
                        self.assertIn(declared, preset.models)


class AttachMediaTest(unittest.TestCase):
    """工具回图 → 历史。链路两端各一条：看得了图和看不了图。"""

    def build(self, vision: tuple[str, ...]):
        from xiaoyu.agent import Agent
        from xiaoyu.config import Config
        from xiaoyu.tools import Toolbox

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Config(
            base_url="http://unused",
            model="main-model",
            workspace=Path(tmp.name).resolve(),
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
            enable_mcp=False,
        )
        registry = providers.Registry(
            [providers.Provider("gateway", "", "", (), "网关", (), vision)],
            clients={"gateway": mock.MagicMock()},
        )
        agent = Agent(config, Toolbox(config), registry=registry)
        ref = media.store(PNG, "image/png")
        agent.toolbox.take_media = lambda: [media.image_part(ref)]  # type: ignore[method-assign]
        return agent

    def test_vision_model_gets_the_image(self):
        agent = self.build(vision=("*",))
        agent._attach_media()
        content = agent.messages[-1]["content"]
        self.assertEqual(agent.messages[-1]["role"], "user")
        self.assertEqual(len(media.images_of(content)), 1)

    def test_blind_model_gets_an_explanation_not_silence(self):
        """静默丢图最坏：模型对着缺了关键内容的结果瞎猜，还不知道自己缺了东西。"""
        agent = self.build(vision=())
        agent._attach_media()
        last = agent.messages[-1]
        self.assertEqual(media.images_of(last["content"]), [])
        self.assertIn("不接受图片输入", media.text_of(last["content"]))

    def test_nothing_appended_when_no_media(self):
        agent = self.build(vision=("*",))
        agent.toolbox.take_media = lambda: []  # type: ignore[method-assign]
        before = len(agent.messages)
        agent._attach_media()
        self.assertEqual(len(agent.messages), before)


if __name__ == "__main__":
    unittest.main()


class InjectedUserTextTest(unittest.TestCase):
    """harness 注入的 user 条目判据全仓一份：四个消费方（子代理蒸馏 / turn_starts /
    TUI 回放 / ACP 回放）都走它。ACP 回放曾自抄一份只认 "[系统提示]"，
    <world_state> 差分（现拼的动态文本，精确集合装不下）一条都拦不住，
    每次 session/load 都把累积的环境播报当用户原话重演一遍。"""

    def _note(self) -> str:
        from xiaoyu import world_state

        return f"{world_state.TAG_OPEN}\n环境有变：\n- 模型：x\n{world_state.TAG_CLOSE}"

    def test_predicate_covers_all_prefixes_and_blank(self):
        for text in ("", "  \n", "[系统提示] 任意", "<system-reminder>\nx\n</system-reminder>",
                     self._note(), "  \n<world_state>带前导空白"):
            self.assertTrue(media.is_injected_user_text(text), repr(text))
        self.assertFalse(media.is_injected_user_text("你好"))
        self.assertFalse(media.is_injected_user_text("讲讲 <world_state> 是什么"))
        self.assertTrue(media.is_injected_user_text("精确文案", frozenset({"精确文案"})))

    def _history(self) -> list[dict]:
        return [
            {"role": "user", "content": "你好"},
            {"role": "user", "content": self._note()},
            {"role": "user", "content": "<system-reminder>\n后台任务完成\n</system-reminder>"},
            {"role": "assistant", "content": "在的"},
        ]

    def test_acp_replay_skips_injected_user_entries(self):
        from types import SimpleNamespace

        from xiaoyu.acp import AcpSink

        frames: list[dict] = []
        server = SimpleNamespace(_notify=lambda m, p: frames.append(p))
        AcpSink(server, "sess-1", Path("/tmp")).replay(self._history())
        users = [p["update"]["content"]["text"] for p in frames
                 if p["update"]["sessionUpdate"] == "user_message_chunk"]
        self.assertEqual(users, ["你好"])

    def test_tui_replay_skips_injected_user_entries(self):
        from xiaoyu import render
        from xiaoyu.events import Notice

        seen: list[str] = []

        class Sink:
            def emit(self, event):
                if isinstance(event, Notice):
                    seen.append(event.text)

        render.replay_transcript(self._history(), Sink())
        user_lines = [t for t in seen if t.startswith("› ")]
        self.assertEqual(user_lines, ["› 你好"])

    def test_turn_starts_skips_injected_user_entries(self):
        self.assertEqual(session_log.turn_starts(self._history(), frozenset()), [0])


class _Reply:
    """一次非流式回复的最小形状（choices[0].message.content + usage）。"""

    def __init__(self, text: str, prompt: int = 11, completion: int = 7) -> None:
        from types import SimpleNamespace

        self.choices = [SimpleNamespace(message=SimpleNamespace(content=text))]
        self.usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


class _ReaderClient:
    """只实现 chat.completions.create 的鸭子 client，把请求录下来供断言。"""

    def __init__(self, reply) -> None:
        from types import SimpleNamespace

        self.requests: list[dict] = []
        self._reply = reply
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


class VisionFallbackTest(unittest.TestCase):
    """代读（XIAOYU_VISION_FALLBACK）：当前模型看不了图时把图换成一段文字。

    两个方向都要钉住——**能代读时别再降级成一行说明**（那是白配），
    **代读用不了时必须原样退回旧行为**（配错/超时不该比没配更糟）。
    """

    def build(self, *, vision=(), fallback="", reply=None, reader_vision=("reader-model",)):
        from xiaoyu.agent import Agent
        from xiaoyu.config import Config
        from xiaoyu.tools import Toolbox

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Config(
            base_url="http://unused",
            model="main-model",
            workspace=Path(tmp.name).resolve(),
            vision_fallback_model=fallback,
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
            enable_mcp=False,
        )
        self.reader = _ReaderClient(reply if reply is not None else _Reply("图片 1：一行报错"))
        registry = providers.Registry(
            [
                providers.Provider("main", "", "", ("main-model",), "主", (), vision),
                providers.Provider(
                    "reader", "", "", ("reader-model", "blind-model"), "代读", (), reader_vision
                ),
            ],
            clients={"main": mock.MagicMock(), "reader": self.reader},
        )
        agent = Agent(config, Toolbox(config), registry=registry)
        ref = media.store(PNG, "image/png")
        agent.toolbox.take_media = lambda: [media.image_part(ref)]  # type: ignore[method-assign]
        return agent

    #  —— 校验：代读模型自己必须收得下图 ——

    def test_reader_must_declare_vision_itself(self):
        """配成一个不收图的型号 = 每张图换来一次 400。校验复用同一套 fail-closed。"""
        agent = self.build(fallback="blind-model")
        self.assertIsNone(agent.registry.vision_reader("blind-model"))
        self.assertIsNone(agent.registry.vision_reader("查无此名"))
        self.assertIsNone(agent.registry.vision_reader(""))
        route = agent.registry.vision_reader("reader-model")
        self.assertIsNotNone(route)
        self.assertEqual(route.qualified, "reader/reader-model")

    def test_unusable_reader_degrades_exactly_like_before(self):
        agent = self.build(fallback="blind-model")
        agent._attach_media()
        self.assertIn("不接受图片输入", media.text_of(agent.messages[-1]["content"]))
        self.assertEqual(self.reader.requests, [])

    #  —— 工具回图：代读收益最大的那条路径（人不在环里）——

    def test_tool_images_become_text(self):
        agent = self.build(fallback="reader-model")
        agent._attach_media()
        last = agent.messages[-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual(media.images_of(last["content"]), [])
        #  抬头必须说清这是转述而不是原图，否则主模型会把它当亲眼所见
        self.assertIn("图片代读", last["content"])
        self.assertIn("reader-model", last["content"])
        #  抬头里刻意用裸名：带斜杠的全限定名长得像路径，会误触发产物对账护栏
        self.assertNotIn("reader/reader-model", last["content"])
        self.assertIn("图片 1：一行报错", last["content"])
        #  图真的发给了代读模型
        sent = self.reader.requests[0]["messages"][0]["content"]
        self.assertEqual(len(media.images_of(sent)), 1)

    def test_guide_is_the_last_real_user_message(self):
        """取景框：同一张截图，问报错和问配色该被转写下来的细节完全不同。"""
        agent = self.build(fallback="reader-model")
        agent.messages.append({"role": "user", "content": "这个报错怎么回事"})
        agent._attach_media()
        prompt = media.text_of(self.reader.requests[0]["messages"][0]["content"])
        self.assertIn("这个报错怎么回事", prompt)

    def test_injected_user_text_is_not_used_as_guide(self):
        from xiaoyu.agent import WRAPUP_INSTRUCTION

        agent = self.build(fallback="reader-model")
        agent.messages.append({"role": "user", "content": "这个报错怎么回事"})
        agent.messages.append({"role": "user", "content": WRAPUP_INSTRUCTION})
        agent._attach_media()
        prompt = media.text_of(self.reader.requests[0]["messages"][0]["content"])
        self.assertIn("这个报错怎么回事", prompt)
        self.assertNotIn("已达到本轮工具调用次数上限", prompt)

    def test_reader_failure_falls_back_to_the_note(self):
        """代读是兜底路径上的兜底：它自己炸了也不能让本轮更糟。"""
        agent = self.build(fallback="reader-model", reply=RuntimeError("上游 429"))
        agent._attach_media()
        self.assertIn("不接受图片输入", media.text_of(agent.messages[-1]["content"]))

    def test_empty_caption_falls_back(self):
        agent = self.build(fallback="reader-model", reply=_Reply("   "))
        agent._attach_media()
        self.assertIn("不接受图片输入", media.text_of(agent.messages[-1]["content"]))

    def test_caption_is_billed(self):
        agent = self.build(fallback="reader-model")
        agent._attach_media()
        self.assertEqual(agent.usage.by_model["reader/reader-model"].prompt_tokens, 11)
        self.assertEqual(agent.usage.by_model["reader/reader-model"].calls, 1)

    #  —— 不该触发的时候别触发 ——

    def test_vision_model_still_gets_the_real_image(self):
        agent = self.build(vision=("*",), fallback="reader-model")
        agent._attach_media()
        self.assertEqual(len(media.images_of(agent.messages[-1]["content"])), 1)
        self.assertEqual(self.reader.requests, [])

    def test_unconfigured_is_a_no_op(self):
        agent = self.build()
        self.assertEqual(agent.caption_images([media.image_part("x")]), ("", ""))
        self.assertEqual(agent.vision_note(), "")

    def test_note_tells_whether_it_will_fire(self):
        self.assertIn("会先转成文字", self.build(fallback="reader-model").vision_note())
        self.assertIn("不会触发", self.build(vision=("*",), fallback="reader-model").vision_note())
        self.assertIn("用不了", self.build(fallback="blind-model").vision_note())

    def test_instruction_gives_no_escape_hatch(self):
        """vision_probe 的第四条纪律：给模型"看不到就明说"的出口 = 给假阴性开门。

        代读指令走的是同一条链路，同一个坑——加了那半句，部分型号会 100%
        自称看不见，代读整个功能静默失效。
        """
        from xiaoyu.agent import VISION_READ_GUIDE, VISION_READ_INSTRUCTION

        for text in (VISION_READ_INSTRUCTION, VISION_READ_GUIDE):
            for hatch in ("看不到", "看不见", "无法看到", "如果你不能"):
                self.assertNotIn(hatch, text)
        #  要的是转写不是评价：产物是主模型的眼睛
        self.assertIn("逐字照抄", VISION_READ_INSTRUCTION)

    #  —— ACP 面：协议面没有"扣住图等重发"的交互，代读在这里比 TUI 值钱 ——

    def acp_degrade(self, agent, text="这个报错怎么回事"):
        from types import SimpleNamespace

        from xiaoyu.acp import _degrade_images

        ref = media.store(PNG, "image/png")
        content = [media.text_part(text), media.image_part(ref)]
        session = SimpleNamespace(agent=agent, sink=agent.sink)
        return _degrade_images(session, content)

    def test_acp_attached_images_become_text(self):
        agent = self.build(fallback="reader-model")
        result = self.acp_degrade(agent)
        self.assertIsInstance(result, str)
        self.assertIn("这个报错怎么回事", result)
        self.assertIn("图片 1：一行报错", result)
        prompt = media.text_of(self.reader.requests[0]["messages"][0]["content"])
        self.assertIn("这个报错怎么回事", prompt)

    def test_acp_without_reader_keeps_the_note(self):
        agent = self.build()
        result = self.acp_degrade(agent)
        self.assertIn("不接受图片输入", result)

    def test_acp_vision_model_keeps_the_parts_untouched(self):
        agent = self.build(vision=("*",), fallback="reader-model")
        result = self.acp_degrade(agent)
        self.assertEqual(len(media.images_of(result)), 1)
