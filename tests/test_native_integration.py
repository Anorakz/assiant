# ============================================================================
#  tests/test_native_integration.py — 真 native 扩展 (.so) 的接线检查
#
#  这个文件在 PC / WSL 上**必然跳过**: moonlight-common-c 只在交叉编译时集成
#  (native/CMakeLists.txt: `if(CMAKE_CROSSCOMPILING)`), 所以 host 那份产物里
#  根本没有 moonlight 符号。它真正跑起来的地方是板端。
#
#  为什么值得单独一个文件: 绑定层的正确性在 C++ 单测里验不到 —— host 构建的产物
#  走的是"未链接 moonlight"那条分支, 于是"这个绑定真的通向库"只能在板端证。
# ============================================================================

import platform
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

#: moonlight 只出现在 aarch64 的交叉编译产物里
_CROSS_ARCHES = ("aarch64", "arm64")


def _skip_reason():
    """不能真验就说明原因; 能验返回 None。"""
    if platform.machine().lower() not in _CROSS_ARCHES:
        return (
            "moonlight 只集成在交叉编译产物里 (native/CMakeLists.txt: "
            "CMAKE_CROSSCOMPILING), 本机是 %s" % platform.machine()
        )
    try:
        import agent_native  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - 缺 .so 是很正常的情况
        return "agent_native 不可用: %s" % exc
    return None


class TestNativeMoonlightBinding(unittest.TestCase):
    """native 绑定接线 (板端才有意义)。"""

    @classmethod
    def setUpClass(cls):
        reason = _skip_reason()
        if reason is not None:
            raise unittest.SkipTest(reason)
        import agent_native

        cls.native = agent_native

    def test_launch_url_query_parameters_exists(self):
        self.assertTrue(callable(self.native.moonlight.launch_url_query_parameters))

    def test_returns_the_library_string(self):
        """必须由 moonlight-common-c 说了算 —— Python 侧不该抄一份常量。"""
        params = self.native.moonlight.launch_url_query_parameters()
        self.assertIsInstance(params, str)
        # Connection.c 里 LiGetLaunchUrlQueryParameters() 的实现:
        #     return "&corever=1";
        self.assertIn("corever", params)
        self.assertTrue(params.startswith("&"), "拼串时按 '&xxx' 追加: %r" % params)

    def test_query_builder_accepts_it(self):
        """agent/net 的拼串要吃下它 (带不带前导 & 都能拼)。"""
        from agent.net import DEFAULT_UNIQUE_ID, build_launch_query

        params = self.native.moonlight.launch_url_query_parameters()
        query = build_launch_query(DEFAULT_UNIQUE_ID, "1", "1280x720x60", params)
        self.assertIn("corever=1", query)
        self.assertFalse(query.endswith("&"), "不能留下悬空的 &: %r" % query)
        self.assertIn("surroundAudioInfo=196610", query)

    def test_status_keeps_its_documented_keys(self):
        """status() 是 Python 侧唯一的状态来源, 键不能悄悄改名。"""
        status = self.native.moonlight.status()
        for key in ("state", "state_code", "connected", "error",
                    "frames_pushed", "image_frames_available"):
            self.assertIn(key, status)

    def test_start_with_session_is_bound(self):
        """B1 的新入口: 握手在外面做完, 这里只接字段。"""
        self.assertTrue(callable(self.native.moonlight.start_with_session))


if __name__ == "__main__":
    unittest.main(verbosity=2)
