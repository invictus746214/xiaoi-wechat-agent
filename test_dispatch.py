import threading
import unittest

from dispatch import Dispatcher


class DispatcherTests(unittest.TestCase):
    def test_one_job_per_conversation(self):
        dispatcher = Dispatcher(lambda payload: payload, workers=2)
        try:
            self.assertTrue(dispatcher.submit("chat-a", "first"))
            self.assertFalse(dispatcher.submit("chat-a", "second"))
            self.assertTrue(dispatcher.submit("chat-b", "other"))
            self.assertTrue(dispatcher.in_flight("chat-a"))
        finally:
            dispatcher.close()

    def test_drain_returns_the_reply_and_frees_the_key(self):
        dispatcher = Dispatcher(lambda payload: payload.upper(), workers=1)
        try:
            dispatcher.submit("chat", "hello")
            results = []
            for _ in range(200):
                results = dispatcher.drain()
                if results:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(len(results), 1)
            key, payload, status, value = results[0]
            self.assertEqual((key, payload, status, value), ("chat", "hello", "reply", "HELLO"))
            self.assertFalse(dispatcher.in_flight("chat"))
        finally:
            dispatcher.close()

    def test_failures_are_reported_not_raised(self):
        def boom(_payload):
            raise RuntimeError("model down")

        dispatcher = Dispatcher(boom, workers=1)
        try:
            dispatcher.submit("chat", "x")
            results = []
            for _ in range(200):
                results = dispatcher.drain()
                if results:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(results[0][2], "error")
            self.assertEqual(results[0][3], "RuntimeError")
        finally:
            dispatcher.close()

    def test_stale_jobs_are_abandoned_without_blocking_the_loop(self):
        release = threading.Event()
        dispatcher = Dispatcher(lambda payload: release.wait(5), workers=1, stale_after=1.0)
        try:
            dispatcher.submit("chat", "x", now=0.0)
            results = dispatcher.drain(now=10.0)
            self.assertEqual(results[0][2], "timeout")
            self.assertIsNone(results[0][3])
            self.assertFalse(dispatcher.in_flight("chat"))
        finally:
            release.set()
            dispatcher.close()
