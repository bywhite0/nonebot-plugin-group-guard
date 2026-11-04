"""消息只允许显式构造安全段，不执行文本中的 CQ。"""

import base64
import importlib.util
import sys
import unittest
from pathlib import Path

from nonebot.adapters.onebot.v11 import Message

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src/nonebot_plugin_group_guard/messages.py"


def load_module():
    if not MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("_guard_messages_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MESSAGES = load_module()


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(MESSAGES, "安全消息构造模块尚未实现")

    def test_cq_in_template_is_literal_text(self):
        text = "欢迎 [CQ:at,qq=all] [CQ:image,file=https://evil.test/x] &amp; &#91;CQ:at,qq=1&#93;"
        message = MESSAGES.to_message(text)
        self.assertIsInstance(message, Message)
        self.assertEqual([segment.type for segment in message], ["text"])
        self.assertEqual(message[0].data["text"], text)
        self.assertEqual(message.extract_plain_text(), text)

    def test_only_supported_placeholders_become_at_segments(self):
        message = MESSAGES.to_message("甲{at_user}乙%at_user%丙{user_id} [CQ:at,qq=all]", at_user=12345)
        self.assertEqual([segment.type for segment in message], ["text", "at", "text", "at", "text"])
        self.assertEqual([segment.data["qq"] for segment in message if segment.type == "at"], ["12345", "12345"])
        self.assertEqual(message.extract_plain_text(), "甲乙丙{user_id} [CQ:at,qq=all]")

    def test_placeholders_are_literal_without_an_at_target(self):
        text = "{at_user} %at_user%"
        message = MESSAGES.to_message(text)
        self.assertEqual([segment.type for segment in message], ["text"])
        self.assertEqual(message.extract_plain_text(), text)

    def test_non_exact_placeholders_remain_text(self):
        text = "{at_user_id} {AT_USER} % at_user% { at_user } %at_user_name%"
        message = MESSAGES.to_message(text, at_user=12345)
        self.assertEqual(message.extract_plain_text(), text)
        self.assertTrue(all(segment.type == "text" for segment in message))

    def test_adjacent_and_edge_placeholders_need_no_empty_text_segments(self):
        message = MESSAGES.to_message("{at_user}%at_user%", at_user=12345)
        self.assertEqual([segment.type for segment in message], ["at", "at"])

    def test_image_bytes_are_encoded_locally_after_text(self):
        image = b"\x89PNG\r\n\x1a\nneutral-test-bytes"
        message = MESSAGES.to_message("图片 [CQ:record,file=evil]", image=image)
        self.assertEqual([segment.type for segment in message], ["text", "image"])
        file = message[-1].data["file"]
        self.assertTrue(file.startswith("base64://"))
        self.assertEqual(base64.b64decode(file.removeprefix("base64://")), image)

    def test_combined_output_has_only_explicit_text_at_and_image(self):
        message = MESSAGES.to_message("{at_user} [CQ:xml,data=evil]", at_user=12345, image=b"test")
        self.assertEqual([segment.type for segment in message], ["at", "text", "image"])
        self.assertEqual(message[1].data["text"], " [CQ:xml,data=evil]")

    def test_empty_message_and_image_only_are_supported(self):
        self.assertEqual(len(MESSAGES.to_message("")), 0)
        self.assertEqual([segment.type for segment in MESSAGES.to_message("", image=b"test")], ["image"])

    def test_image_urls_cannot_sneak_through_bytes_parameter(self):
        with self.assertRaises(TypeError):
            MESSAGES.to_message("", image="https://evil.test/image.png")


if __name__ == "__main__":
    unittest.main()
