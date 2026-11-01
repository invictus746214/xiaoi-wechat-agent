import subprocess
import unittest
from unittest.mock import patch

import ocr


class SimilarityTests(unittest.TestCase):
    def test_identical_text_scores_one(self):
        self.assertEqual(ocr.similarity("你好世界", "你好世界"), 1.0)

    def test_whitespace_and_case_are_ignored(self):
        self.assertEqual(ocr.similarity("Hello  World", "hello world"), 1.0)

    def test_substring_scores_partially(self):
        # whitespace is stripped before comparing, so "hello" sits inside "helloworld"
        self.assertAlmostEqual(ocr.similarity("hello", "hello world"), 0.5, places=3)

    def test_related_text_scores_high(self):
        self.assertGreaterEqual(ocr.similarity("明天见", "明天見"), 0.66)

    def test_unrelated_text_scores_low(self):
        self.assertLess(ocr.similarity("明天见", "今天晚上吃什么"), 0.3)

    def test_empty_input_scores_zero(self):
        self.assertEqual(ocr.similarity("", "abc"), 0.0)
        self.assertEqual(ocr.similarity("abc", ""), 0.0)


class PreviewMatchTests(unittest.TestCase):
    def test_truncated_preview_matches_its_message(self):
        self.assertTrue(ocr.matches_preview("哈哈这个我真说不了，我又没那个本事", "哈哈这个我真说不了，我又没…"))

    def test_ocr_noise_still_matches(self):
        self.assertTrue(ocr.matches_preview("哈哈这个我真说不了，我又没那个本事", "哈哈这个我克提不了，我又没…"))

    def test_unrelated_preview_does_not_match(self):
        self.assertFalse(ocr.matches_preview("明天见", "今天晚上吃什么"))

    def test_very_short_preview_is_not_trusted(self):
        self.assertFalse(ocr.matches_preview("你好", "你"))

    def test_empty_preview_does_not_match(self):
        self.assertFalse(ocr.matches_preview("你好", ""))


class ReadTests(unittest.TestCase):
    def test_successful_read_returns_stdout(self):
        reader = ocr.Ocr(binary="/bin/true")
        with patch("ocr.shutil.which", return_value="/bin/true"), patch("ocr.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="  识别结果  ".encode("utf-8"), stderr=b"")
            self.assertEqual(reader.read("/tmp/x.png"), "识别结果")

    def test_non_zero_exit_returns_empty(self):
        reader = ocr.Ocr(binary="/bin/true")
        with patch("ocr.shutil.which", return_value="/bin/true"), patch("ocr.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"boom")
            self.assertEqual(reader.read("/tmp/x.png"), "")

    def test_missing_binary_disables_reading(self):
        reader = ocr.Ocr(binary="/definitely/not/here")
        with patch("ocr.shutil.which", return_value=None):
            self.assertFalse(reader.available())
            self.assertEqual(reader.read("/tmp/x.png"), "")

    def test_timeout_is_swallowed(self):
        reader = ocr.Ocr(binary="/bin/true")
        with patch("ocr.shutil.which", return_value="/bin/true"), patch("ocr.subprocess.run", side_effect=subprocess.TimeoutExpired("t", 1)):
            self.assertEqual(reader.read("/tmp/x.png"), "")


if __name__ == "__main__":
    unittest.main()
