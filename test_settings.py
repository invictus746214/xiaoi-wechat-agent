import json
import os
import tempfile
import unittest

import settings


class NormalizeTests(unittest.TestCase):
    def test_defaults_preserve_the_deployed_behaviour(self):
        config = settings.normalize({})
        self.assertEqual(config["api"]["model"], "deepseek-flash")
        self.assertFalse(config["proactive"]["enabled"])
        self.assertFalse(config["moments"]["enabled"])
        self.assertEqual(config["policy"]["whitelist"], [])
        self.assertFalse(config["policy"]["group_mention_only"])
        self.assertEqual(config["memory"]["recent_turns"], 40)
        self.assertEqual(config["memory"]["summary_every"], 60)

    def test_partial_config_is_merged_not_replaced(self):
        config = settings.normalize({"api": {"model": "custom"}, "memory": {"enabled": False}})
        self.assertEqual(config["api"]["model"], "custom")
        self.assertEqual(config["api"]["timeout"], 45)
        self.assertFalse(config["memory"]["enabled"])

    def test_zero_workers_means_synchronous_mode(self):
        self.assertEqual(settings.normalize({"dispatch": {"workers": 0}})["dispatch"]["workers"], 0)

    def test_out_of_range_numbers_are_clamped(self):
        config = settings.normalize({"memory": {"recent_turns": 999}, "dispatch": {"workers": -3}})
        self.assertEqual(config["memory"]["recent_turns"], 80)
        self.assertEqual(config["dispatch"]["workers"], 0)

    def test_greetings_need_all_three_fields(self):
        config = settings.normalize({"proactive": {"greetings": [
            {"at": "09:00", "contact": "Alice", "text": "早"},
            {"at": "09:00", "contact": "Alice"},
            "garbage",
        ]}})
        self.assertEqual(config["proactive"]["greetings"], [{"at": "09:00", "contact": "Alice", "text": "早"}])

    def test_unknown_fields_do_not_break_loading(self):
        config = settings.normalize({"whatever": {"deep": 1}})
        self.assertEqual(config["api"]["model"], "deepseek-flash")

    def test_broken_input_falls_back_to_defaults(self):
        self.assertEqual(settings.normalize(None)["api"]["model"], "deepseek-flash")
        self.assertEqual(settings.normalize("nonsense")["api"]["model"], "deepseek-flash")

    def test_config_file_with_a_bom_still_loads(self):
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        try:
            with open(path, "wb") as target:
                target.write(b"\xef\xbb\xbf" + json.dumps({"api": {"model": "bom-model"}}).encode("utf-8"))
            self.assertEqual(settings.load(path)["api"]["model"], "bom-model")
        finally:
            os.unlink(path)
