"""测试包级隔离：把用户配置目录指到一次性临时目录。

开发机的真实 ~/.config/xiaoyu 里往往有 mcp.json、schema 缓存、用户级 .env——
不隔离的话本机的 MCP server / 配置会混进 Toolbox 和 load_server_specs，
测试变成"干净机器上必过、开发机上莫名失败"。集中在包入口做一次，
胜过每个用例各自 patch user_config_dir（漏一个就复发）。

user_config_dir() 在调用时才读环境变量，所以这里改 env 对所有导入顺序都生效；
个别用例（test_user_config）自己 patch/pop 这些变量时照常覆盖此基线。
"""

import atexit
import os
import tempfile

_isolated = tempfile.TemporaryDirectory(prefix="xiaoyu-test-config-")
atexit.register(_isolated.cleanup)
#  posix 走 XDG_CONFIG_HOME，Windows 走 APPDATA，两个都指过去
os.environ["XDG_CONFIG_HOME"] = _isolated.name
os.environ["APPDATA"] = _isolated.name
