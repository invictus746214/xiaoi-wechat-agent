import time
import unittest

import extras


def at(hour, minute=0):
    return time.mktime((2026, 1, 1, hour, minute, 0, 0, 0, -1))


class Node:
    def __init__(self, name="", role="", children=()):
        self.name = name
        self._role = role
        self._children = list(children)

    @property
    def childCount(self):
        return len(self._children)

    def __getitem__(self, index):
        return self._children[index]

    def getRoleName(self):
        return self._role


class HeartbeatTests(unittest.TestCase):
    def test_disabled_heartbeat_never_fires(self):
        self.assertFalse(extras.heartbeat_due(None, 0, 0))

    def test_heartbeat_respects_the_interval(self):
        self.assertTrue(extras.heartbeat_due(None, 100, 30))
        self.assertFalse(extras.heartbeat_due(100, 100 + 60, 30))
        self.assertTrue(extras.heartbeat_due(100, 100 + 1800, 30))


class GreetingTests(unittest.TestCase):
    def test_greeting_fires_inside_the_window_only(self):
        greetings = [{"at": "09:00", "contact": "Alice", "text": "早"}]
        self.assertEqual(len(extras.due_greetings(greetings, at(9, 5), set())), 1)
        self.assertEqual(len(extras.due_greetings(greetings, at(9, 30), set())), 0)
        self.assertEqual(len(extras.due_greetings(greetings, at(8, 55), set())), 0)

    def test_greeting_is_sent_once_per_day(self):
        greetings = [{"at": "09:00", "contact": "Alice", "text": "早"}]
        due = extras.due_greetings(greetings, at(9, 1), set())
        key = due[0][0]
        self.assertEqual(extras.due_greetings(greetings, at(9, 2), {key}), [])

    def test_empty_webhook_is_a_no_op(self):
        self.assertFalse(extras.send_webhook("", "hello"))


class MomentsProbeTests(unittest.TestCase):
    def test_moments_entry_is_found(self):
        tree = Node("微信", "frame", [Node("朋友圈", "push button")])
        self.assertEqual(extras.probe_moments(tree), {"name": "朋友圈", "role": "push button"})

    def test_missing_entry_returns_none(self):
        tree = Node("微信", "frame", [Node("聊天", "label")])
        self.assertIsNone(extras.probe_moments(tree))

    def test_probe_is_bounded(self):
        deep = Node("root", "frame")
        node = deep
        for _ in range(50):
            child = Node("x", "label")
            node._children.append(child)
            node = child
        self.assertIsNone(extras.probe_moments(deep, limit=10))
