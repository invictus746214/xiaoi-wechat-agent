"""Vision request shape and fail-closed behaviour."""
import base64
import os
import tempfile
import unittest
import json
from unittest.mock import patch

import vision


class PayloadTests(unittest.TestCase):
    def test_deepseek_caption_disables_hidden_reasoning_only_for_official_endpoint(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_): pass
            def read(self): return json.dumps({'choices':[{'message':{'content':'黄色卡通角色在跳动'},'finish_reason':'stop'}]}).encode()
        for host,expected in [('api.deepseek.com',True),('other.example',False)]:
            with patch('vision.encode_image',return_value='QUJD'), patch('vision.urlopen',return_value=Response()) as send:
                result=vision.describe({'model':'deepseek-flash','url':'https://'+host+'/chat/completions','prompt':'p'},['image.png'],'test')
                payload=json.loads(send.call_args.args[0].data)
                self.assertEqual('thinking' in payload,expected)
                if expected: self.assertEqual(payload['thinking'],{'type':'disabled'})
                self.assertIn('黄色',result)

    def test_empty_caption_is_reported_without_exposing_response_or_key(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_): pass
            def read(self): return b'{"choices":[{"message":{"content":"","reasoning_content":"private"},"finish_reason":"length"}]}'
        with patch('vision.encode_image',return_value='QUJD'),patch('vision.urlopen',return_value=Response()),self.assertLogs('wechat-autoreply',level='WARNING') as logs:
            self.assertEqual(vision.describe({'model':'m','url':'https://other.example','prompt':'p'},'image','secret-key'),'')
        self.assertIn('vision_response_empty finish=length',str(logs.output))
        self.assertNotIn('private',str(logs.output))
        self.assertNotIn('secret-key',str(logs.output))
    def test_payload_is_openai_compatible(self):
        payload = vision.build_payload("m", "QUJD", "描述一下", 120)
        self.assertEqual("m", payload["model"])
        self.assertEqual(120, payload["max_tokens"])
        content = payload["messages"][0]["content"]
        self.assertEqual("描述一下", content[0]["text"])
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_animated_sticker_frames_share_one_request(self):
        content = vision.build_payload("m", ["QUJD", "REVG", "R0hJ"], "看图")["messages"][0]["content"]
        self.assertEqual(3, len([part for part in content if part["type"] == "image_url"]))

    def test_encode_image_roundtrip(self):
        handle, path = tempfile.mkstemp(suffix=".png")
        os.close(handle)
        try:
            with open(path, "wb") as target:
                target.write(b"hello")
            self.assertEqual(base64.b64encode(b"hello").decode(), vision.encode_image(path))
        finally:
            os.remove(path)

    def test_missing_key_returns_empty(self):
        self.assertEqual("", vision.describe({"model": "m", "url": "http://127.0.0.1:1",
                                              "prompt": "p", "max_tokens": 10}, "/nope", ""))

    def test_missing_file_returns_empty(self):
        cfg = {"model": "m", "url": "http://127.0.0.1:1", "prompt": "p", "max_tokens": 10}
        self.assertEqual("", vision.describe(cfg, "/definitely/not/here.png", "key"))


if __name__ == "__main__":
    unittest.main()
