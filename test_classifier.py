import os
import tempfile
import unittest

from bridge import is_media_stub, load_api_key


class BackgroundClassifierTests(unittest.TestCase):
    def test_media_placeholders_are_not_replied_to(self):
        self.assertTrue(is_media_stub("[图片]"))
        self.assertTrue(is_media_stub("群友：张三：语音"))
        self.assertFalse(is_media_stub("图片怎么发"))
        self.assertFalse(is_media_stub("图片：怎么样"))

    def test_key_file_is_parsed_as_data(self):
        handle, path = tempfile.mkstemp(suffix=".env")
        os.close(handle)
        try:
            value = "sk-test$();not-shell-code"
            with open(path, "w", encoding="utf-8") as target:
                target.write("DEEPSEEK_API_KEY=" + value + "\n")
            self.assertEqual(load_api_key(path), value)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
