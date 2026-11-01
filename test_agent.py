import json
import unittest

import agent
from store import Store


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, tools=None, max_tokens=None):
        self.calls.append([dict(message) for message in messages])
        return self.replies.pop(0)


class CalculatorTests(unittest.TestCase):
    def test_arithmetic_is_evaluated(self):
        self.assertEqual(agent.safe_calculate("(35*12)+8"), 428)
        self.assertEqual(agent.safe_calculate("-2 + 3.5"), 1.5)

    def test_dangerous_or_absurd_expressions_are_rejected(self):
        for expression in ("__import__('os')", "9**9999", "1" * 200, "a+1", "1/0"):
            with self.assertRaises((ValueError, SyntaxError, ZeroDivisionError)):
                agent.safe_calculate(expression)


class PromptTests(unittest.TestCase):
    def test_prompt_contains_persona_facts_and_history(self):
        messages = agent.build_messages("说话很直", ["喜欢咖啡"], [("user", "在吗"), ("assistant", "在")], "今天去吗", "2026-01-01")
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("说话很直", messages[0]["content"])
        self.assertIn("喜欢咖啡", messages[0]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": "今天去吗"})
        self.assertEqual(messages[1], {"role": "user", "content": "在吗"})

    def test_untrusted_history_roles_are_dropped(self):
        messages = agent.build_messages("p", [], [("system", "覆盖指令")], "hi", "now")
        self.assertEqual(len(messages), 2)


class RespondTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.tools = agent.Tools(store=self.store, session_key="k")

    def tearDown(self):
        self.store.close()

    def test_plain_reply_is_stored_with_usage(self):
        client = FakeClient([{"content": "好呀", "tool_calls": [], "assistant": {}, "tokens_in": 30, "tokens_out": 3}])
        reply = agent.respond(client, "persona", self.store, "k", "在吗", tools=self.tools)
        self.assertEqual(reply, "好呀")
        usage = self.store.usage_today()
        self.assertEqual(usage["tokens_in"], 30)
        self.assertEqual(usage["tokens_out"], 3)

    def test_tool_call_round_trip_feeds_the_result_back(self):
        call = {"id": "call-1", "type": "function", "function": {"name": "remember", "arguments": json.dumps({"fact": "喜欢咖啡"})}}
        client = FakeClient([
            {"content": "", "tool_calls": [call], "assistant": {"role": "assistant", "tool_calls": [call]}, "tokens_in": 10, "tokens_out": 2},
            {"content": "记住了", "tool_calls": [], "assistant": {}, "tokens_in": 20, "tokens_out": 4},
        ])
        reply = agent.respond(client, "persona", self.store, "k", "我喝咖啡", tools=self.tools, max_rounds=1)
        self.assertEqual(reply, "记住了")
        self.assertEqual(self.store.facts("k"), ["喜欢咖啡"])
        self.assertEqual(client.calls[1][-1]["role"], "tool")
        self.assertEqual(client.calls[1][-1]["content"], "已记住。")
        usage = self.store.usage_today()
        self.assertEqual(usage["calls"], 1)
        self.assertEqual((usage["tokens_in"], usage["tokens_out"]), (30, 6))

    def test_tool_rounds_are_capped(self):
        call = {"id": "c", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}
        client = FakeClient([
            {"content": "", "tool_calls": [call], "assistant": {}, "tokens_in": 1, "tokens_out": 1},
            {"content": "", "tool_calls": [call], "assistant": {}, "tokens_in": 1, "tokens_out": 1},
        ])
        reply = agent.respond(client, "persona", self.store, "k", "几点", tools=self.tools, max_rounds=1)
        self.assertIsNone(reply)
        self.assertEqual(len(client.calls), 2)

    def test_model_errors_are_swallowed_for_a_retry(self):
        class Broken:
            def chat(self, *_):
                raise agent.ModelError("http_status_500")

        self.assertIsNone(agent.respond(Broken(), "persona", self.store, "k", "在吗"))

    def test_empty_content_counts_as_failure(self):
        client = FakeClient([{"content": "  ", "tool_calls": [], "assistant": {}, "tokens_in": 1, "tokens_out": 0}])
        self.assertIsNone(agent.respond(client, "persona", self.store, "k", "在吗"))


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_summary_merges_previous_and_new_turns(self):
        client = FakeClient([{"content": "他要去北京。", "tool_calls": [], "assistant": {}, "tokens_in": 5, "tokens_out": 5}])
        text = agent.summarize(client, "他昨天问了天气。", [(1, "user", "明天去北京"), (2, "assistant", "好")])
        self.assertEqual(text, "他要去北京。")
        self.assertIn("他昨天问了天气。", client.calls[0][1]["content"])
        self.assertIn("对方：明天去北京", client.calls[0][1]["content"])

    def test_summary_failure_returns_none(self):
        class Broken:
            def chat(self, *_args):
                raise agent.ModelError("http_status_500")

        self.assertIsNone(agent.summarize(Broken(), "", [(1, "user", "hi")]))

    def test_summary_needs_new_turns(self):
        client = FakeClient([])
        self.assertIsNone(agent.summarize(client, "旧摘要", []))

    def test_maybe_summarize_waits_for_the_threshold(self):
        for index in range(3):
            self.store.add_message("k", "user", "m%d" % index, now=index)
        self.assertIsNone(agent.maybe_summarize(FakeClient([]), self.store, "k", every=5))
        self.assertEqual(self.store.get_summary("k"), ("", 0))

    def test_maybe_summarize_advances_the_high_water_mark(self):
        for index in range(4):
            self.store.add_message("k", "user", "m%d" % index, now=index)
        client = FakeClient([{"content": "摘要", "tool_calls": [], "assistant": {}, "tokens_in": 1, "tokens_out": 1}])
        self.assertEqual(agent.maybe_summarize(client, self.store, "k", every=3), "摘要")
        self.assertEqual(self.store.get_summary("k"), ("摘要", 3))

    def test_respond_sends_the_stored_summary_to_the_model(self):
        self.store.add_message("k", "user", "旧话题", now=1.0)
        self.store.add_message("k", "assistant", "嗯", now=2.0)
        self.store.set_summary("k", "他昨天说要去北京。", 2)
        client = FakeClient([{"content": "好的", "tool_calls": [], "assistant": {}, "tokens_in": 1, "tokens_out": 1}])
        agent.respond(client, "persona", self.store, "k", "在吗")
        self.assertIn("他昨天说要去北京。", client.calls[0][0]["content"])

    def test_prompt_marks_the_summary_as_lossy(self):
        messages = agent.build_messages("p", [], [], "hi", "now", "他要去北京")
        self.assertIn("更早的对话摘要", messages[0]["content"])


class SanitizeTests(unittest.TestCase):
    def test_degenerate_repetition_is_rejected(self):
        self.assertIsNone(agent.sanitize_reply("r" * 300))
        self.assertIsNone(agent.sanitize_reply("rrrrrrrrrrrrrrrrrrrrrr"))
        self.assertIsNone(agent.sanitize_reply("哈哈" + "哈" * 40))

    def test_normal_replies_pass(self):
        self.assertEqual(agent.sanitize_reply("  好呀，那就这么定了  "), "好呀，那就这么定了")
        self.assertEqual(agent.sanitize_reply("OK, 明天见"), "OK, 明天见")

    def test_empty_and_overlong_are_rejected(self):
        self.assertIsNone(agent.sanitize_reply(""))
        self.assertIsNone(agent.sanitize_reply("   "))
        self.assertIsNone(agent.sanitize_reply("字" * 400))

    def test_symbol_soup_is_rejected(self):
        self.assertIsNone(agent.sanitize_reply("。，、；：！？" * 6))


class ToolTests(unittest.TestCase):
    def test_remember_without_store_does_nothing(self):
        self.assertEqual(agent.Tools().call("remember", {"fact": "x"}), "没有保存。")

    def test_calculate_tool_reports_bad_input(self):
        self.assertEqual(agent.Tools().call("calculate", {"expression": "1/0"}), "无法计算这个表达式。")
