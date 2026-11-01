import time
import unittest

import policy


def at(hour, minute=0):
    return time.mktime((2026, 1, 1, hour, minute, 0, 0, 0, -1))


class QuietHoursTests(unittest.TestCase):
    def test_plain_window(self):
        ranges = [["23:00", "08:00"]]
        self.assertTrue(policy.in_quiet_hours(ranges, at(23, 30)))
        self.assertTrue(policy.in_quiet_hours(ranges, at(7, 59)))
        self.assertFalse(policy.in_quiet_hours(ranges, at(8, 0)))
        self.assertFalse(policy.in_quiet_hours(ranges, at(12, 0)))

    def test_same_day_window(self):
        ranges = [["12:00", "13:30"]]
        self.assertTrue(policy.in_quiet_hours(ranges, at(12, 0)))
        self.assertFalse(policy.in_quiet_hours(ranges, at(14, 0)))

    def test_malformed_ranges_are_ignored(self):
        self.assertFalse(policy.in_quiet_hours([["25:00", "08:00"], "x", ["09:00"]], at(23, 30)))
        self.assertFalse(policy.in_quiet_hours([], at(23, 30)))


class AllowTests(unittest.TestCase):
    def test_default_policy_allows_everything(self):
        self.assertEqual(policy.allowed({}, {"kind": "group", "name": "群"}, at(3, 0)), (True, "ok"))

    def test_whitelist_narrows_to_listed_contacts(self):
        config = {"whitelist": ["Alice"]}
        self.assertEqual(policy.allowed(config, {"kind": "private", "name": "Alice"}), (True, "ok"))
        self.assertEqual(policy.allowed(config, {"kind": "private", "name": "Bob"}), (False, "not_whitelisted"))

    def test_group_mention_gate_only_applies_to_groups(self):
        config = {"group_mention_only": True}
        self.assertEqual(policy.allowed(config, {"kind": "group", "name": "群"}), (False, "group_not_mentioned"))
        self.assertEqual(policy.allowed(config, {"kind": "group", "name": "群", "is_mention": True}), (True, "ok"))
        self.assertEqual(policy.allowed(config, {"kind": "group", "name": "群", "engaged": True}), (True, "ok"))
        self.assertEqual(policy.allowed(config, {"kind": "group", "name": "群", "interject": True}), (True, "ok"))
        self.assertEqual(policy.allowed(config, {"kind": "private", "name": "Alice"}), (True, "ok"))

    def test_group_always_reply_overrides_mention_gate(self):
        config = {"group_mention_only": True, "group_always_reply": ["添砖"]}
        self.assertEqual(policy.allowed(config, {"kind": "group", "name": "添砖"}), (True, "ok"))
        self.assertEqual(policy.allowed(config, {"kind": "group", "name": "别的群"}), (False, "group_not_mentioned"))

    def test_system_accounts_are_never_answered(self):
        config = {"skip_contacts": ["Weixin Team", "Tencent News"]}
        self.assertEqual(policy.allowed(config, {"kind": "private", "name": "Weixin Team"}), (False, "skipped_contact"))
        self.assertEqual(policy.allowed(config, {"kind": "private", "name": "Alice"}), (True, "ok"))

    def test_quiet_hours_win_over_whitelist(self):
        config = {"whitelist": ["Alice"], "quiet_hours": [["00:00", "07:00"]]}
        self.assertEqual(policy.allowed(config, {"kind": "private", "name": "Alice"}, at(2, 0)), (False, "quiet_hours"))
