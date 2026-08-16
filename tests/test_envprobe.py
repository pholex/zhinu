"""环境画像探测的测试：工具链清单、网络区域启发式、system prompt 段落。"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from xiaoyu import envprobe


class ProbeToolsTest(unittest.TestCase):
    def test_present_and_missing_partition_all_names(self) -> None:
        present, missing = envprobe.probe_tools()
        expected = set(envprobe._COMMON_TOOLS) | set(
            envprobe._WINDOWS_TOOLS if os.name == "nt" else envprobe._POSIX_TOOLS
        )
        self.assertEqual(set(present) | set(missing), expected)
        self.assertFalse(set(present) & set(missing))

    def test_missing_tool_reported(self) -> None:
        with mock.patch.object(envprobe.shutil, "which", return_value=None):
            present, missing = envprobe.probe_tools()
        self.assertEqual(present, [])
        self.assertIn("git", missing)


class ChinaNetworkTest(unittest.TestCase):
    def test_zh_cn_lang_hits(self) -> None:
        with mock.patch.dict(os.environ, {"LANG": "zh_CN.UTF-8", "LC_ALL": ""}):
            self.assertTrue(envprobe.china_network_likely())

    def test_tz_hits(self) -> None:
        with mock.patch.dict(
            os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": "", "TZ": "Asia/Shanghai"}
        ):
            self.assertTrue(envprobe.china_network_likely())

    def test_non_china_environment_misses(self) -> None:
        #  宿主机可能就在中国：locale 与系统时区都要一并 mock 掉才是干净的反例
        with mock.patch.dict(
            os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": "", "TZ": "America/New_York"}
        ), mock.patch.object(
            envprobe.locale, "getlocale", return_value=("en_US", "UTF-8")
        ), mock.patch.object(envprobe.time, "timezone", 18000), mock.patch.object(
            envprobe.time, "daylight", 1
        ):
            self.assertFalse(envprobe.china_network_likely())


class BlockTest(unittest.TestCase):
    def test_block_mentions_missing_tools(self) -> None:
        with mock.patch.object(
            envprobe, "probe_tools", return_value=(["curl"], ["git", "node"])
        ), mock.patch.object(envprobe, "china_network_likely", return_value=False):
            text = envprobe.block()
        self.assertIn("环境画像", text)
        self.assertIn("git、node", text)
        self.assertIn("不要走到一半才发现缺", text)
        self.assertNotIn("中国大陆", text)

    def test_block_warns_about_china_cdn(self) -> None:
        with mock.patch.object(
            envprobe, "probe_tools", return_value=(["git"], [])
        ), mock.patch.object(envprobe, "china_network_likely", return_value=True):
            text = envprobe.block()
        self.assertIn("unpkg", text)
        self.assertIn("内联", text)
        self.assertNotIn("未检测到", text)


if __name__ == "__main__":
    unittest.main()
