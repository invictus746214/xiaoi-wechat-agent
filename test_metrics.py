import json
import os
import time
import unittest

import metrics

HERE = os.path.dirname(os.path.abspath(__file__))


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(HERE, "_metrics_test.jsonl")
        self._remove()

    def tearDown(self):
        self._remove()

    def _remove(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_event_is_written_as_one_json_line(self):
        recorder = metrics.Metrics(self.path)
        self.assertTrue(recorder.event("reply_sent", latency_ms=1200, chars=42))
        with open(self.path, encoding="utf-8") as source:
            record = json.loads(source.readline())
        self.assertEqual(record["event"], "reply_sent")
        self.assertEqual(record["latency_ms"], 1200)
        self.assertIn("ts", record)

    def test_none_fields_are_dropped(self):
        metrics.Metrics(self.path).event("x", a=None, b=1)
        with open(self.path, encoding="utf-8") as source:
            record = json.loads(source.readline())
        self.assertNotIn("a", record)
        self.assertEqual(record["b"], 1)

    def test_disabled_metrics_write_nothing(self):
        recorder = metrics.Metrics(self.path, enabled=False)
        self.assertFalse(recorder.event("x"))
        self.assertFalse(os.path.exists(self.path))

    def test_summary_counts_and_percentile(self):
        now = time.time()
        with open(self.path, "w", encoding="utf-8") as target:
            for latency in (100, 200, 300, 400):
                target.write(json.dumps({"ts": now, "event": "reply_sent", "latency_ms": latency}) + "\n")
            target.write(json.dumps({"ts": now, "event": "skipped", "reason": "quiet_hours"}) + "\n")
            target.write(json.dumps({"ts": now - 999999, "event": "old"}) + "\n")
            target.write("not json\n")
        summary = metrics.summarize(self.path, days=1, now=now)
        self.assertEqual(summary["counts"]["reply_sent"], 4)
        self.assertEqual(summary["counts"]["skipped"], 1)
        self.assertNotIn("old", summary["counts"])
        self.assertEqual(summary["reasons"]["quiet_hours"], 1)
        self.assertEqual(summary["reply_p95_ms"], 400)
        self.assertEqual(summary["reply_avg_ms"], 250)

    def test_cache_hit_ratio_is_aggregated(self):
        now = time.time()
        with open(self.path, "w", encoding="utf-8") as target:
            target.write(json.dumps({"ts": now, "event": "model_usage", "tokens_in": 3000, "cached": 2000}) + "\n")
            target.write(json.dumps({"ts": now, "event": "model_usage", "tokens_in": 1000, "cached": 0}) + "\n")
        summary = metrics.summarize(self.path, days=1, now=now)
        self.assertEqual(summary["input_tokens"], 4000)
        self.assertEqual(summary["cached_tokens"], 2000)
        self.assertEqual(summary["cache_hit_ratio"], 0.5)

    def test_missing_file_is_not_an_error(self):
        summary = metrics.summarize(os.path.join(HERE, "does-not-exist.jsonl"))
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["counts"], {})


if __name__ == "__main__":
    unittest.main()
