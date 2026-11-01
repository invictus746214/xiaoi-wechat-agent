"""Tests for the Moments planner: policy, caps, dedupe and dry-run."""
import unittest

import moments_agent
import moments_feed
import policy
import settings
from store import Store


def post(author, text, index=0, liked=False):
    return moments_feed.Post(
        id=moments_feed.post_id(author, text),
        author=author,
        text=text,
        liked=liked,
        rect=(0, index * 100, 400, 100),
        like_rect=(10, index * 100 + 60, 40, 20),
        comment_rect=(60, index * 100 + 60, 60, 20),
        index=index,
    )


class FakeFeed:
    def __init__(self, posts, like_result="liked", comment_result="commented"):
        self.posts = posts
        self.like_result = like_result
        self.comment_result = comment_result
        self.calls = []

    def scan(self, pages=1, hover=True):
        return list(self.posts)

    def iter_pages(self, pages=1, hover=True):
        yield self.scan(pages=pages, hover=hover)

    def like(self, item):
        self.calls.append(("like", item.id))
        return self.like_result

    def comment(self, item, text):
        self.calls.append(("comment", item.id, text))
        return self.comment_result


def config(**moments):
    base = {
        "moments": {
            "enabled": True,
            "dry_run": True,
            "like_enabled": True,
            "comment_enabled": False,
            "authors": [],
            "skip_authors": [],
            "quiet_hours": [],
            "like_daily_cap": 20,
            "comment_daily_cap": 5,
            "max_posts": 10,
            "scan_pages": 1,
            "scan_minutes": 30,
            "min_interval_seconds": 0,
        }
    }
    base["moments"].update(moments)
    return settings.normalize(base)


class PolicyTests(unittest.TestCase):
    def test_every_switch_must_be_on(self):
        cfg = config(enabled=False)["moments"]
        allowed, reason = policy.moment_allowed(cfg, post("张三", "你好"), "like", False, 0)
        self.assertFalse(allowed)
        self.assertEqual("disabled", reason)

    def test_author_allowlist_and_skip(self):
        cfg = config(authors=["李四"])["moments"]
        self.assertEqual("author_not_allowed",
                         policy.moment_allowed(cfg, post("张三", "你好"), "like", False, 0)[1])
        cfg = config(skip_authors=["张三"])["moments"]
        self.assertEqual("author_skipped",
                         policy.moment_allowed(cfg, post("张三", "你好"), "like", False, 0)[1])

    def test_daily_cap_and_done(self):
        cfg = config(like_daily_cap=2)["moments"]
        self.assertFalse(policy.moment_allowed(cfg, post("张三", "你好"), "like", False, 2)[0])
        self.assertEqual("already_done",
                         policy.moment_allowed(cfg, post("张三", "你好"), "like", True, 0)[1])

    def test_keyword_skip(self):
        cfg = config(skip_keywords=["广告"])["moments"]
        self.assertEqual("keyword_skipped",
                         policy.moment_allowed(cfg, post("张三", "这是广告"), "like", False, 0)[1])

    def test_missing_author_allowed_without_allowlist(self):
        cfg = config()["moments"]
        allowed, reason = policy.moment_allowed(cfg, post("", "只有内容"), "like", False, 0)
        self.assertTrue(allowed, reason)

    def test_quiet_hours(self):
        cfg = config(quiet_hours=[["00:00", "23:59"]])["moments"]
        self.assertEqual("quiet_hours",
                         policy.moment_allowed(cfg, post("张三", "你好"), "like", False, 0)[1])


class RunnerTests(unittest.TestCase):
    def test_each_page_is_acted_on_before_scrolling(self):
        first, second = post("张三", "第一页"), post("李四", "第二页")
        class PagedFeed(FakeFeed):
            def iter_pages(self, pages=1, hover=True):
                self.visible = {first.id}
                yield [first]
                self.visible = {second.id}
                yield [second, first]

            def like(self, item):
                self.assert_visible(item)
                return super().like(item)

            def assert_visible(self, item):
                if item.id not in self.visible:
                    raise AssertionError("action ran after leaving its page")

        feed = PagedFeed([])
        report = moments_agent.Runner(feed, config(dry_run=False, scan_pages=2),
                                      pause=lambda _s: None).run()
        self.assertEqual([first.id, second.id], report.liked)
        self.assertEqual(2, report.scanned)

    def test_dry_run_never_touches_the_feed(self):
        feed = FakeFeed([post("张三", "今天天气不错")])
        runner = moments_agent.Runner(feed, config(), store=None, pause=lambda _s: None)
        report = runner.run()
        self.assertTrue(report.dry_run)
        self.assertEqual([], feed.calls)
        self.assertEqual(1, len(report.liked))
        self.assertEqual(1, report.scanned)

    def test_like_is_recorded_and_never_repeated(self):
        store = Store(":memory:")
        target = post("张三", "今天天气不错")
        feed = FakeFeed([target])
        cfg = config(dry_run=False)
        report = moments_agent.Runner(feed, cfg, store=store, pause=lambda _s: None).run()
        self.assertEqual([target.id], report.liked)
        self.assertTrue(store.moment_done(target.id, "like"))
        again = moments_agent.Runner(feed, cfg, store=store, pause=lambda _s: None).run()
        self.assertEqual([], again.liked)
        self.assertIn((target.id, "like", "already_done"), again.skipped)

    def test_daily_cap_blocks_the_second_post(self):
        store = Store(":memory:")
        feed = FakeFeed([post("张三", "一"), post("李四", "二", 1)])
        cfg = config(dry_run=False, like_daily_cap=1)
        report = moments_agent.Runner(feed, cfg, store=store, pause=lambda _s: None).run()
        self.assertEqual(1, len(report.liked))
        self.assertIn("daily_cap", [item[2] for item in report.skipped])

    def test_failed_action_is_not_recorded(self):
        store = Store(":memory:")
        target = post("张三", "今天天气不错")
        feed = FakeFeed([target], like_result="uncertain")
        report = moments_agent.Runner(feed, config(dry_run=False), store=store,
                                      pause=lambda _s: None).run()
        self.assertEqual([], report.liked)
        self.assertIn("failed_uncertain", [item[2] for item in report.skipped])
        self.assertFalse(store.moment_done(target.id, "like"))

    def test_uncertain_is_never_retried(self):
        store = Store(":memory:")
        target = post("张三", "今天天气不错")
        feed = FakeFeed([target], like_result="uncertain")
        cfg = config(dry_run=False)
        moments_agent.Runner(feed, cfg, store=store, pause=lambda _s: None).run()
        self.assertTrue(store.moment_done(target.id, "like_uncertain"))
        calls = len(feed.calls)
        again = moments_agent.Runner(feed, cfg, store=store, pause=lambda _s: None).run()
        self.assertEqual(calls, len(feed.calls))
        self.assertIn((target.id, "like", "already_done"), again.skipped)

    def test_already_liked_is_recorded(self):
        store = Store(":memory:")
        target = post("张三", "今天天气不错")
        feed = FakeFeed([target], like_result="already")
        cfg = config(dry_run=False)
        report = moments_agent.Runner(feed, cfg, store=store, pause=lambda _s: None).run()
        self.assertIn("already", [item[2] for item in report.skipped])
        self.assertTrue(store.moment_done(target.id, "like"))

    def test_disabled_comment_is_skipped(self):
        feed = FakeFeed([post("张三", "今天天气不错")])
        report = moments_agent.Runner(feed, config(), store=None, pause=lambda _s: None).run()
        self.assertIn("comment_disabled", [item[2] for item in report.skipped])

    def test_comment_uses_generated_text_and_confirms(self):
        store = Store(":memory:")
        target = post("张三", "今天天气不错")
        feed = FakeFeed([target])
        cfg = config(dry_run=False, comment_enabled=True)
        runner = moments_agent.Runner(feed, cfg, store=store, commenter=lambda _p: "确实不错",
                                      pause=lambda _s: None)
        report = runner.run()
        self.assertEqual([target.id], report.commented)
        self.assertIn(("comment", target.id, "确实不错"), feed.calls)
        self.assertTrue(store.moment_done(target.id, "comment"))

    def test_empty_comment_is_skipped(self):
        feed = FakeFeed([post("张三", "今天天气不错")])
        cfg = config(dry_run=False, comment_enabled=True, like_enabled=False)
        report = moments_agent.Runner(feed, cfg, store=None, commenter=lambda _p: "",
                                      pause=lambda _s: None).run()
        self.assertIn("empty_comment", [item[2] for item in report.skipped])
        self.assertEqual([], feed.calls)

    def test_max_posts_limits_the_scan(self):
        feed = FakeFeed([post("张三", "一"), post("李四", "二", 1), post("王五", "三", 2)])
        cfg = config(max_posts=2)
        report = moments_agent.Runner(feed, cfg, store=None, pause=lambda _s: None).run()
        self.assertEqual(2, report.scanned)


class CommentTextTests(unittest.TestCase):
    def test_clean_comment_rejects_junk(self):
        self.assertEqual("确实不错", moments_agent.clean_comment("“确实不错”"))
        self.assertEqual("", moments_agent.clean_comment(""))
        self.assertEqual("", moments_agent.clean_comment("看这个 https://x.cn"))
        self.assertEqual("", moments_agent.clean_comment("字" * 60))


if __name__ == "__main__":
    unittest.main()
