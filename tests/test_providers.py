"""多 provider 路由：合并、优先级、影子兜底、显式寻址。不打网络。"""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest import mock

import openai

from xiaoyu import providers
from xiaoyu.agent import Agent, Usage
from xiaoyu.config import Config, MissingConfig, find_api_key
from xiaoyu.providers import GATEWAY, Provider, Registry, Route, UnknownModel
from xiaoyu.responses import Transport

from .test_agent_paths import FakeClient, chunk, usage_chunk
from .test_errors import _response

GW = "https://gw.example/v1"
DS_MODELS = providers.PRESETS["deepseek"].models

#  clear=True 只为隔离 provider 探测相关的环境变量，它模拟的应是"没配任何
#  provider 变量"，而不是"操作系统身份也被抹掉"——真实机器的环境从来不会
#  真空。两类必须保留：
#  ① 配置目录隔离基线（tests/__init__ 设的 XDG_CONFIG_HOME/APPDATA）——
#     清掉它，Windows 上 user_config_dir() 退到 Path.home()（环境被清空时抛
#     RuntimeError），posix 上漏读开发机真实 ~/.config/xiaoyu；
#  ② Windows 系统级变量——OpenSSL 3.x 初始化要 SYSTEMROOT，缺了构造 OpenAI
#     client 时 SSLContext 直接 SSLError 0xa080024（py3.14 实测，py3.11 的老
#     OpenSSL 不踩），临时目录要 TEMP/TMP。与 xiaoyu/mcp.py 给子进程保留
#     系统变量的清单是同一个道理。
_KEEP = (
    "XDG_CONFIG_HOME", "APPDATA",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "OS",
    "TEMP", "TMP", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
)
_ISOLATION = {key: os.environ[key] for key in _KEEP if key in os.environ}


def isolated_env(extra: dict[str, str] | None = None):
    """clear=True 的替身：环境清空但保留配置目录隔离基线。"""
    return mock.patch.dict(os.environ, {**_ISOLATION, **(extra or {})}, clear=True)


def config(**kw) -> Config:
    kw.setdefault("base_url", GW)
    kw.setdefault("model", "deepseek-v4-pro")
    kw.setdefault("workspace", Path.cwd())
    #  与其他测试文件同一纪律：不扫测试机的技能库/插件。这里以前没关——
    #  构造 Agent 会经 scan_skills() 摸 Path.home()，在环境被清空的 Windows
    #  上直接炸（CI 红了三轮的另一半根因），posix 上则静默混入开发机技能。
    kw.setdefault("enable_skills", False)
    kw.setdefault("enable_plugins", False)
    return Config(**kw)


class ProviderTestCase(unittest.TestCase):
    """每个用例都在干净的环境里跑，且绝不真的去读本机 Keychain。"""

    ENV: dict[str, str] = {}

    def setUp(self) -> None:
        #  不打桩的话 find_api_key 会真的 shell out 去 security 里翻——
        #  慢，而且开发机上真有 key 时用例会莫名其妙地"通过"。
        patcher = mock.patch("xiaoyu.config._read_from_keychain", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = isolated_env(self.ENV)
        env.start()
        self.addCleanup(env.stop)


# ---------- 只有网关：必须与改动前完全等价 ----------


class TestGatewayOnly(ProviderTestCase):
    """Step 1 的回归闸门：单端点形态下，路由层不许改变任何行为。"""

    ENV = {"XIAOYU_API_KEY": "k"}

    def setUp(self) -> None:
        super().setUp()
        self.registry = providers.build(config())

    def test_only_gateway_registered(self) -> None:
        self.assertEqual([p.name for p in self.registry.providers], [GATEWAY])

    def test_any_model_falls_to_gateway(self) -> None:
        for name in ("deepseek-v4-pro", "gateway-only-model", "没见过的模型"):
            route = self.registry.resolve(name)
            self.assertEqual(route.provider, GATEWAY)
            self.assertEqual(route.model, name, "透传给网关的必须是原始名字")

    def test_no_backups_so_chain_equals_old_behaviour(self) -> None:
        """旧 model_chain 是「主模型 + fallback_models 去重」，这里必须一模一样。"""
        self.assertEqual(self.registry.backups("deepseek-v4-pro"), [])
        cfg = config(fallback_models=["b", "deepseek-v4-pro", "c"])
        agent = Agent(cfg, registry=self.registry, usage=Usage())
        self.assertEqual(
            [route.model for route in agent.model_chain()],
            ["deepseek-v4-pro", "b", "c"],
            "重复项要去掉，顺序要保持——和旧实现同一份语义",
        )

    def test_sticky_name_stays_bare(self) -> None:
        """只有一家时粘性降级写回的必须还是裸名，/model 显示不该变样。"""
        route = self.registry.resolve("backup-model")
        self.assertEqual(self.registry.sticky_name(route), "backup-model")

    def test_gateway_without_key_is_not_registered(self) -> None:
        with isolated_env({}):
            with self.assertRaises(MissingConfig):
                providers.build(config())


# ---------- 直连 + 网关：合并与优先级 ----------


class TestMerge(ProviderTestCase):
    ENV = {"XIAOYU_API_KEY": "gw", "DEEPSEEK_API_KEY": "ds"}

    def setUp(self) -> None:
        super().setUp()
        self.registry = providers.build(config())

    def test_direct_first(self) -> None:
        self.assertEqual([p.name for p in self.registry.providers], ["deepseek", GATEWAY])

    def test_direct_wins_for_claimed_models(self) -> None:
        for name in DS_MODELS:
            self.assertEqual(self.registry.resolve(name).provider, "deepseek")

    def test_gateway_keeps_everything_else(self) -> None:
        self.assertEqual(self.registry.resolve("gateway-only-model").provider, GATEWAY)

    def test_shadow_backup_only_for_shared_names(self) -> None:
        shared = [route.provider for route in self.registry.backups(DS_MODELS[0])]
        self.assertEqual(shared, [GATEWAY], "同名模型的网关那份要留作兜底")
        self.assertEqual(
            self.registry.backups("gateway-only-model"),
            [],
            "网关自己就是最后一家，后面没人可兜底",
        )

    def test_listing_dedupes_by_first_owner(self) -> None:
        entries = self.registry.listing()
        self.assertEqual([e.model for e in entries], list(DS_MODELS))
        self.assertTrue(all(e.owner == "deepseek" for e in entries))
        self.assertTrue(all(e.backups == (GATEWAY,) for e in entries))

    def test_describe_shows_source_and_backup(self) -> None:
        text = self.registry.describe()
        self.assertIn("直连 deepseek", text)
        self.assertIn("兜底", text)
        self.assertIn("其余任意模型名", text, "通配 provider 无法枚举，但必须告诉用户它在")

    def test_chain_puts_backup_right_after_primary(self) -> None:
        cfg = config(fallback_models=["gateway-only-model"])
        agent = Agent(cfg, registry=self.registry, usage=Usage())
        self.assertEqual(
            [route.qualified for route in agent.model_chain()],
            [
                "deepseek/deepseek-v4-pro",
                f"{GATEWAY}/deepseek-v4-pro",
                f"{GATEWAY}/gateway-only-model",
            ],
        )

    def test_chain_dedupes_by_provider_and_model(self) -> None:
        """备用链里重复写了主模型/兜底目标时不该重复尝试。"""
        cfg = config(fallback_models=["deepseek-v4-pro", f"{GATEWAY}/deepseek-v4-pro"])
        agent = Agent(cfg, registry=self.registry, usage=Usage())
        self.assertEqual(
            [route.qualified for route in agent.model_chain()],
            ["deepseek/deepseek-v4-pro", f"{GATEWAY}/deepseek-v4-pro"],
        )

    def test_unknown_fallback_is_skipped_not_fatal(self) -> None:
        """备用链里写错一个名字，不该让每次请求都炸——网关通配时本来也不会走到这。"""
        registry = Registry([Provider("deepseek", "u", "k", DS_MODELS, "直连 deepseek")])
        agent = Agent(
            config(model="deepseek-v4-pro", fallback_models=["打错的名字"]),
            registry=registry,
            usage=Usage(),
        )
        self.assertEqual([r.model for r in agent.model_chain()], ["deepseek-v4-pro"])

    def test_unknown_primary_raises(self) -> None:
        """主模型解析不了是必须立刻看见的配置错误，不能静默。"""
        registry = Registry([Provider("deepseek", "u", "k", DS_MODELS, "直连 deepseek")])
        agent = Agent(config(model="打错的名字"), registry=registry, usage=Usage())
        with self.assertRaises(UnknownModel):
            agent.model_chain()


# ---------- 显式寻址 ----------


class TestExplicitAddressing(ProviderTestCase):
    ENV = {"XIAOYU_API_KEY": "gw", "DEEPSEEK_API_KEY": "ds"}

    def setUp(self) -> None:
        super().setUp()
        self.registry = providers.build(config())

    def test_prefix_pins_the_provider(self) -> None:
        route = self.registry.resolve(f"{GATEWAY}/deepseek-v4-pro")
        self.assertEqual(route.provider, GATEWAY)
        self.assertEqual(route.model, "deepseek-v4-pro", "前缀不能跟着发给上游")

    def test_unknown_prefix_passes_through_whole_name(self) -> None:
        """网关上真有 anthropic/claude-x 这种自带斜杠的模型名，不许误切。"""
        route = self.registry.resolve("anthropic/claude-x")
        self.assertEqual(route.provider, GATEWAY)
        self.assertEqual(route.model, "anthropic/claude-x")

    def test_pinned_route_gets_no_backup(self) -> None:
        self.assertEqual(self.registry.backups(f"{GATEWAY}/deepseek-v4-pro"), [])

    def test_sticky_name_qualifies_only_when_needed(self) -> None:
        gw = self.registry.resolve(f"{GATEWAY}/deepseek-v4-pro")
        #  裸名会被直连抢走，所以必须写全限定名，否则 /model 显示的和实际跑的对不上
        self.assertEqual(self.registry.sticky_name(gw), f"{GATEWAY}/deepseek-v4-pro")
        direct = self.registry.resolve("deepseek-v4-pro")
        self.assertEqual(self.registry.sticky_name(direct), "deepseek-v4-pro")


# ---------- 顺序覆盖、key 解析、通用兜底 ----------


class TestOrderAndKeys(ProviderTestCase):
    def test_xiaoyu_providers_overrides_order(self) -> None:
        env = {
            "XIAOYU_API_KEY": "gw",
            "DEEPSEEK_API_KEY": "ds",
            "XIAOYU_PROVIDERS": f"{GATEWAY},deepseek",
        }
        with isolated_env(env):
            registry = providers.build(config())
        self.assertEqual([p.name for p in registry.providers], [GATEWAY, "deepseek"])
        #  网关通配且排在前面，于是它把所有名字都吃掉——这正是默认不这么排的理由
        self.assertEqual(registry.resolve("deepseek-v4-pro").provider, GATEWAY)
        #  清单的归属必须跟着翻转，否则 /model 显示的和实际跑的对不上
        entry = next(e for e in registry.listing() if e.model == "deepseek-v4-pro")
        self.assertEqual(entry.owner, GATEWAY)
        self.assertEqual(entry.backups, ("deepseek",), "反过来之后直连成了兜底")

    def test_direct_only_needs_no_gateway(self) -> None:
        """"只填一个 key 就能跑"——本次改动最主要的动机。

        键名就是厂商原生的 DEEPSEEK_API_KEY：用户机器上常常早就配过了，直接复用。
        """
        with isolated_env({"DEEPSEEK_API_KEY": "ds"}):
            registry = providers.build(config(base_url=""))
        self.assertEqual(registry.resolve("deepseek-v4-pro").provider, "deepseek")
        with self.assertRaises(UnknownModel):
            registry.resolve("gateway-only-model")

    def test_gateway_accepts_litellm_env_name(self) -> None:
        """网关 key 也认 LiteLLM 生态惯用的 LITELLM_API_KEY——为别的工具配过的直接复用。"""
        with isolated_env({"LITELLM_API_KEY": "gw"}):
            registry = providers.build(config())
        self.assertEqual([p.name for p in registry.providers], [GATEWAY])

    def test_provider_without_key_is_dropped(self) -> None:
        """没 key 就不注册——否则配置错误会推迟到用户已经开始对话之后才炸。"""
        with isolated_env({"XIAOYU_API_KEY": "gw"}):
            registry = providers.build(config())
        self.assertEqual([p.name for p in registry.providers], [GATEWAY])

    def test_generic_env_provider_is_discovered(self) -> None:
        #  minimax 是"未内置厂商"的例子（moonshot 已升级为内置 preset，不能再当例子用）
        env = {
            "XIAOYU_API_KEY": "gw",
            "XIAOYU_PROVIDER_MINIMAX_BASE_URL": "https://mm.example/v1",
            "XIAOYU_PROVIDER_MINIMAX_API_KEY": "mm",
            "XIAOYU_PROVIDER_MINIMAX_MODELS": "minimax-m2, minimax-m2-turbo",
        }
        with isolated_env(env):
            registry = providers.build(config())
        self.assertEqual([p.name for p in registry.providers], ["minimax", GATEWAY])
        self.assertEqual(registry.resolve("minimax-m2").provider, "minimax")
        self.assertEqual([r.provider for r in registry.backups("minimax-m2")], [GATEWAY])

    def test_domestic_presets_register_from_vendor_env_names(self) -> None:
        """moonshot / qwen / zhipu 三家 preset：厂商原生键名即插即用。"""
        env = {
            "MOONSHOT_API_KEY": "ms",
            "QWEN_API_KEY": "qw",
            "ZHIPU_API_KEY": "zp",
        }
        with isolated_env(env):
            registry = providers.build(config(base_url=""))
        self.assertEqual(
            [p.name for p in registry.providers], ["moonshot", "qwen", "zhipu"]
        )
        self.assertEqual(registry.resolve("kimi-k3").provider, "moonshot")
        self.assertEqual(registry.resolve("qwen3.8-max").provider, "qwen")
        self.assertEqual(registry.resolve("glm-5.3").provider, "zhipu")

    def test_qwen_accepts_dashscope_env_name(self) -> None:
        """阿里生态惯用 DASHSCOPE_API_KEY——为别的工具配过的直接复用。"""
        with isolated_env({"DASHSCOPE_API_KEY": "ds"}):
            registry = providers.build(config(base_url=""))
        self.assertEqual([p.name for p in registry.providers], ["qwen"])

    def test_overseas_presets_register_from_vendor_env_names(self) -> None:
        """openai / anthropic 海外 preset：厂商原生键名即插即用。"""
        env = {
            "OPENAI_API_KEY": "oa",
            "ANTHROPIC_API_KEY": "an",
        }
        with isolated_env(env):
            registry = providers.build(config(base_url=""))
        self.assertEqual([p.name for p in registry.providers], ["anthropic", "openai"])
        self.assertEqual(registry.resolve("gpt-5.6-sol").provider, "openai")
        self.assertEqual(registry.resolve("claude-opus-5").provider, "anthropic")

    def test_openai_client_speaks_responses_protocol(self) -> None:
        """openai 直连必须说 Responses：gpt-5.6 线在 chat completions 上带 tools
        就 400，接错等于每一轮都炸。anthropic 直连说原生 Messages（OpenAI 兼容
        端点是评估用的：无缓存、effort 被忽略、thinking 不回传，见 messages.py）。
        两家都仍要过 Transport——出网口只有一个，私有键才摘得干净。"""
        with isolated_env({"OPENAI_API_KEY": "oa", "ANTHROPIC_API_KEY": "an"}):
            registry = providers.build(config(base_url=""))
            self.assertEqual(
                registry.client("openai").protocol_for("gpt-5.6-sol"), "responses"
            )
            anthropic = registry.client("anthropic")
            self.assertEqual(anthropic.protocol_for("claude-opus-5"), "anthropic")
            self.assertIsInstance(anthropic._inner, openai.OpenAI)

    def test_deepseek_speaks_responses_only_on_flash(self) -> None:
        """v4-pro 的 /responses 明确"稍后开放"，而它是默认主模型——协议按型号
        声明就是为了这种一家两制，按家切会当场把主模型切死。
        vision-exp（flash 底座）实测 /responses 通且回 encrypted reasoning，随 flash 选边。"""
        with isolated_env({"DEEPSEEK_API_KEY": "ds"}):
            client = providers.build(config(base_url="")).client("deepseek")
            self.assertEqual(client.protocol_for("deepseek-v4-flash"), "responses")
            self.assertEqual(client.protocol_for("deepseek-v4-flash-vision-exp"), "responses")
            self.assertEqual(client.protocol_for("deepseek-v4-pro"), "chat")

    def test_deepseek_vision_only_on_vision_exp(self) -> None:
        """vision_models 只写实测过的：vision-exp 绿/紫两轮全对（2026-08-24），
        pro/flash 仍不收图（fail-closed，误声明=工具回图时每轮 400）。"""
        with isolated_env({"DEEPSEEK_API_KEY": "ds"}):
            registry = providers.build(config(base_url=""))
            self.assertTrue(registry.sees_images("deepseek-v4-flash-vision-exp"))
            self.assertFalse(registry.sees_images("deepseek-v4-flash"))
            self.assertFalse(registry.sees_images("deepseek-v4-pro"))

    def test_generic_provider_can_opt_into_responses(self) -> None:
        """未内置的厂商也能自救：PROTOCOL=responses，不必等我们补 preset。"""
        env = {
            "XIAOYU_PROVIDER_ACME_BASE_URL": "https://acme.example/v1",
            "XIAOYU_PROVIDER_ACME_API_KEY": "k",
            "XIAOYU_PROVIDER_ACME_PROTOCOL": "responses",
        }
        with isolated_env(env):
            registry = providers.build(config(base_url=""))
            self.assertEqual(registry.client("acme").protocol_for("任意型号"), "responses")

    def test_generic_provider_can_opt_into_anthropic(self) -> None:
        """PROTOCOL=anthropic：Bedrock Mantle 一类"Claude 只挂原生协议"端点的逃生舱。"""
        env = {
            "XIAOYU_PROVIDER_MANTLE_BASE_URL": "https://mantle.example/v1",
            "XIAOYU_PROVIDER_MANTLE_API_KEY": "k",
            "XIAOYU_PROVIDER_MANTLE_PROTOCOL": "anthropic",
        }
        with isolated_env(env):
            registry = providers.build(config(base_url=""))
            self.assertEqual(registry.client("mantle").protocol_for("任意型号"), "anthropic")

    def test_keychain_service_name_equals_env_name(self) -> None:
        """Keychain service 名 = 变量名本身：.env / 环境变量 / Keychain 三处同名。

        网关的 XIAOYU_API_KEY 恰好也是历史 service 名，老用户存的 key 天然有效。
        """
        with isolated_env({}), mock.patch(
            "xiaoyu.config._read_from_keychain",
            side_effect=lambda service: {"DEEPSEEK_API_KEY": "kc"}.get(service),
        ):
            self.assertEqual(find_api_key(providers.PRESETS["deepseek"].key_envs), "kc")
            self.assertIsNone(find_api_key(("XIAOYU_API_KEY",)), "网关查的是自己的名字")

    def test_env_beats_keychain(self) -> None:
        with isolated_env({"DEEPSEEK_API_KEY": "env"}), mock.patch(
            "xiaoyu.config._read_from_keychain", return_value="kc"
        ):
            self.assertEqual(find_api_key(providers.PRESETS["deepseek"].key_envs), "env")

    def test_missing_everything_hints_the_short_path(self) -> None:
        with isolated_env({}):
            with self.assertRaises(MissingConfig) as caught:
                providers.build(config(base_url=""))
        self.assertIn("DEEPSEEK_API_KEY", str(caught.exception))


# ---------- /model 无参的网关现场探测 ----------


def _models_client(ids: list[str]):
    """带 /v1/models 的假 client：with_options 返回自身，list 返回带 .id 的对象。"""
    import types

    client = types.SimpleNamespace()
    client.with_options = lambda **kw: client
    client.models = types.SimpleNamespace(
        list=lambda: [types.SimpleNamespace(id=i) for i in ids]
    )
    return client


class TestRemoteModels(ProviderTestCase):
    """现场探测只问通配 provider；失败降级为 None，绝不把异常抛进 REPL。"""

    def registry_with(self, gateway_client) -> Registry:
        #  直连 provider 的 client 故意放个裸 object：remote_models 若碰它必炸，
        #  用例通过即证明"只探测通配 provider"
        return Registry(
            [
                Provider("deepseek", "u", "k", DS_MODELS, "直连 deepseek"),
                Provider(GATEWAY, "u", "k", (), "网关"),
            ],
            clients={"deepseek": object(), GATEWAY: gateway_client},
        )

    def test_probes_only_wildcards_and_sorts(self) -> None:
        registry = self.registry_with(_models_client(["b-model", "a-model"]))
        self.assertEqual(
            registry.remote_models(), [("网关", ["a-model", "b-model"], "")]
        )

    def test_failure_degrades_with_reason(self) -> None:
        """失败要带一句原因：预算 429 和网关挂了对用户是完全不同的两件事。"""
        broken = _models_client([])
        broken.models.list = mock.Mock(side_effect=RuntimeError("Budget has been exceeded"))
        registry = self.registry_with(broken)
        self.assertEqual(
            registry.remote_models(), [("网关", None, "Budget has been exceeded")]
        )

    def test_probe_feeds_completion_cache(self) -> None:
        """探测成功后清单进缓存，/model 补全才补得出网关模型。"""
        registry = self.registry_with(_models_client(["b-model", "a-model"]))
        self.assertEqual(registry.remote_cached(), [], "未探测时缓存为空")
        registry.remote_models()
        self.assertEqual(registry.remote_cached(), ["a-model", "b-model"])

    def test_probe_failure_keeps_stale_cache(self) -> None:
        """失败不清旧缓存：补全候选宁可略旧也别突然清零。"""
        client = _models_client(["a-model"])
        registry = self.registry_with(client)
        registry.remote_models()
        client.models.list = mock.Mock(side_effect=RuntimeError("boom"))
        registry.remote_models()
        self.assertEqual(registry.remote_cached(), ["a-model"])


# ---------- Route / client 缓存 ----------


# ---------- 跨 provider 兜底真的会发生 ----------


class TestCrossProviderFallback(ProviderTestCase):
    """网关从单点变成兜底——这是整件事最大的收益，必须有用例锁住。"""

    ENV = {"XIAOYU_API_KEY": "gw", "DEEPSEEK_API_KEY": "ds"}

    def build(self, direct_script: list, gateway_script: list) -> tuple[Agent, FakeClient]:
        direct, gateway = FakeClient(direct_script), FakeClient(gateway_script)
        registry = Registry(
            [
                Provider("deepseek", "u", "k", DS_MODELS, "直连 deepseek"),
                Provider(GATEWAY, "u", "k", (), "网关"),
            ],
            clients={"deepseek": direct, GATEWAY: gateway},
        )
        cfg = config(auto_approve=True, enable_skills=False, enable_plugins=False)
        return Agent(cfg, registry=registry, usage=Usage()), gateway

    def _run(self, agent: Agent) -> None:
        with mock.patch("xiaoyu.agent.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")

    def test_rate_limited_direct_falls_to_gateway(self) -> None:
        limited = openai.RateLimitError("429", response=_response(429), body=None)
        agent, gateway = self.build([limited] * 3, [[chunk(content="网关顶上")]])
        self._run(agent)
        self.assertEqual(agent.last_assistant_text(), "网关顶上")
        self.assertEqual(gateway.completions.calls[0]["model"], "deepseek-v4-pro")
        #  粘性写回必须是全限定名，否则裸名又会被直连抢走，等于没切
        self.assertEqual(agent.config.model, f"{GATEWAY}/deepseek-v4-pro")

    def test_dead_direct_key_falls_to_gateway(self) -> None:
        """直连 key 过期 / 额度用光——鉴权错误同一家换模型没用，换一家才有用。"""
        dead = openai.AuthenticationError("401", response=_response(401), body=None)
        agent, gateway = self.build([dead], [[chunk(content="网关顶上")]])
        self._run(agent)
        self.assertEqual(agent.last_assistant_text(), "网关顶上")
        self.assertEqual(len(gateway.completions.calls), 1)

    def test_quota_exhausted_direct_falls_to_gateway(self) -> None:
        """直连额度用尽（quota，非限流）：同家重试无解，跨家兜底该顶上，
        且不该在直连上退避重试——额度等不来。"""
        quota = openai.RateLimitError(
            "You exceeded your current quota, please check your plan and billing details.",
            response=_response(429),
            body=None,
        )
        agent, gateway = self.build([quota], [[chunk(content="网关顶上")]])
        self._run(agent)
        self.assertEqual(agent.last_assistant_text(), "网关顶上")
        #  直连只被打了一次（无退避重试），网关一次成功
        self.assertEqual(len(gateway.completions.calls), 1)

    def test_auth_failure_on_last_provider_still_raises(self) -> None:
        """没有别家可试时鉴权错误照旧直接抛——那是配置问题，不该假装在恢复。"""
        dead = openai.AuthenticationError("401", response=_response(401), body=None)
        registry = Registry.for_client(FakeClient([dead]))
        agent = Agent(config(model="m"), registry=registry, usage=Usage())
        with self.assertRaises(openai.AuthenticationError):
            self._run(agent)

    def test_usage_is_split_by_provider(self) -> None:
        """同名模型跑在两家上，账必须分开——否则"直连省了多少"根本算不出来。"""
        limited = openai.RateLimitError("429", response=_response(429), body=None)
        agent, _ = self.build(
            [limited] * 3, [[chunk(content="ok"), usage_chunk(100, 10)]]
        )
        self._run(agent)
        self.assertEqual(list(agent.usage.by_model), [f"{GATEWAY}/deepseek-v4-pro"])


class TestRouteAndClients(ProviderTestCase):
    ENV = {"XIAOYU_API_KEY": "gw", "DEEPSEEK_API_KEY": "ds"}

    def test_route_equality_ignores_client(self) -> None:
        """去重按 (provider, model)：client 参与比较的话去重就失效了。"""
        self.assertEqual(Route("a", "m", object()), Route("a", "m", object()))

    def test_client_is_cached_per_provider(self) -> None:
        registry = providers.build(config())
        with mock.patch("xiaoyu.providers.OpenAI") as fake:
            fake.side_effect = lambda **kw: object()
            first = registry.client("deepseek")
            second = registry.client("deepseek")
        self.assertIs(first, second, "同一家只开一条连接池")
        self.assertEqual(fake.call_count, 1)

    def test_api_key_not_in_repr(self) -> None:
        """异常回溯会带出 repr，key 绝不能出现在里面。"""
        provider = Provider("x", "https://u", "super-secret", (), "x")
        self.assertNotIn("super-secret", repr(provider))


if __name__ == "__main__":
    unittest.main()
