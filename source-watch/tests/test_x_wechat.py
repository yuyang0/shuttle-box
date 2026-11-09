"""X 与微信模块：去重、落盘、配置分派；桌面部分用假读数替换。"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from modules import base as mod_base      # noqa: E402
from modules import wechat, x             # noqa: E402


class ModuleCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def ctx(self, name: str, **config):
        return mod_base.Ctx(name=name, config={"every": "1d", **config}, outdir=self.root,
                            last_success=None, force=False)

    def state(self) -> dict:
        path = self.root / "state.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def not_due(self) -> mod_base.Ctx:
        ctx = self.ctx("x")
        ctx.last_success = datetime.now().astimezone()
        return ctx


POSTS = [
    {"handle": "Alpha", "author": "Alpha", "time": "5 minutes ago", "text": "第一条",
     "url": "https://x.com/Alpha/status/111", "engagement": {"likes": 3}},
    {"handle": "Beta", "author": "Beta", "time": "1 hour ago", "text": "第二条",
     "url": "https://x.com/Beta/status/222"},
]


class XTests(ModuleCase):
    def setUp(self):
        super().setUp()
        self.home_calls = []
        self.account_calls = []
        self.addCleanup(setattr, x, "read_home", x.read_home)
        self.addCleanup(setattr, x, "read_account", x.read_account)

    def feed(self, posts):
        def fake_home(tab="", screens=6, window=None):
            self.home_calls.append((tab, screens, window))
            return "Home / X", posts

        def fake_account(handle, screens=6, window=None):
            self.account_calls.append((handle, screens, window))
            return "Beta / X", posts

        x.read_home = fake_home
        x.read_account = fake_account

    def test_first_run_primes_without_writing(self):
        self.feed(POSTS)
        self.assertEqual(x.run(self.ctx("x", tabs=["Following"], accounts=["@Beta"])), [])
        state = self.state()
        self.assertTrue(state["primed"])
        self.assertEqual(state["seen"], ["111", "222"])
        self.assertFalse((self.root / "timeline").exists())

    def test_new_posts_render_sections(self):
        self.feed(POSTS)
        config = {"tabs": ["Following"], "accounts": ["https://x.com/Beta"]}
        x.run(self.ctx("x", **config))
        home_new = {"handle": "G", "author": "Gamma", "time": "now",
                    "text": "时间线里的新帖", "url": "https://x.com/G/status/333"}
        account_new = {"handle": "H", "author": "Hotel", "time": "now",
                       "text": "账号里的新帖", "url": "https://x.com/H/status/444"}
        x.read_home = lambda tab="", screens=6, window=None: ("Home / X", POSTS + [home_new])
        x.read_account = lambda handle, screens=6, window=None: ("Beta / X", POSTS + [account_new])
        files = x.run(self.ctx("x", **config))
        self.assertEqual(len(files), 1)
        body = files[0].read_text()
        self.assertIn("## Home · Following", body)
        self.assertIn("## @Beta", body)
        self.assertIn("时间线里的新帖", body)
        self.assertIn("账号里的新帖", body)
        self.assertNotIn("第一条", body)
        self.assertNotIn("### Alpha", body)               # 已读的帖子不再出现
        seen = self.state()["seen"]
        self.assertIn("333", seen)
        self.assertIn("444", seen)

    def test_no_sections_reads_home(self):
        self.feed(POSTS)
        x.run(self.ctx("x", tabs=[], accounts=[]))
        self.assertEqual(self.home_calls, [("", 6, None)])
        self.assertEqual(self.account_calls, [])

    def test_config_reaches_readers(self):
        self.feed(POSTS)
        x.run(self.ctx("x", tabs=["Following"], accounts=["@Beta"], screens=2, window="Home"))
        self.assertEqual(self.home_calls[0], ("Following", 2, "Home"))
        self.assertEqual(self.account_calls[0], ("@Beta", 2, "Home"))

    def test_handle_of(self):
        self.assertEqual(x.handle_of("@foo"), "foo")
        self.assertEqual(x.handle_of("https://x.com/foo"), "foo")
        self.assertEqual(x.handle_of("https://twitter.com/foo"), "foo")

    def test_not_due_never_reads(self):
        x.read_home = lambda *a, **k: self.fail("未到期不该驱动 Firefox")
        x.read_account = lambda *a, **k: self.fail("未到期不该驱动 Firefox")
        self.assertEqual(x.run(self.not_due()), [])

    def test_reader_failure_propagates(self):
        x.read_home = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no Firefox window"))
        with self.assertRaises(RuntimeError):
            x.run(self.ctx("x"))

    def test_cli_uses_config_by_default(self):
        self.feed(POSTS)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = x.main([], config={"tabs": ["Following"], "accounts": ["@Beta"], "screens": 2})
        self.assertEqual(code, 0)
        self.assertEqual(self.home_calls, [("Following", 2, None)])
        self.assertEqual(self.account_calls, [("@Beta", 2, None)])
        self.assertEqual([s["source"] for s in json.loads(out.getvalue())],
                         ["Home · Following", "@Beta"])

    def test_cli_overrides_config(self):
        self.feed(POSTS)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            x.main(["--tab", "Investment", "--screens", "1"],
                   config={"tabs": ["Following"], "accounts": ["@Beta"], "screens": 4})
        self.assertEqual(self.home_calls, [("Investment", 1, None)])
        self.assertEqual(self.account_calls, [("@Beta", 1, None)])   # 没覆盖的仍取配置


WX_ARTICLES = [
    {"account": "Kevin策略研究", "title": "文章一", "url": "https://mp.weixin.qq.com/s/AbCdEfGh",
     "paragraphs": ["第一段", "第二段"]},
    {"account": "Kevin策略研究", "title": "文章二", "url": None, "paragraphs": ["只有一段"]},
]


class WeChatTests(ModuleCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.addCleanup(setattr, wechat, "read_articles", wechat.read_articles)
        self.addCleanup(setattr, wechat, "require_wechat", wechat.require_wechat)
        wechat.require_wechat = lambda: None

    def feed(self, articles):
        def fake(**kwargs):
            self.calls.append(kwargs)
            return articles

        wechat.read_articles = fake

    def test_feed_and_accounts_conflict(self):
        self.feed(WX_ARTICLES)
        with self.assertRaises(RuntimeError):
            wechat.run(self.ctx("wechat", feed=True, accounts=["Kevin策略研究"]))

    def test_missing_accounts(self):
        with self.assertRaises(RuntimeError):
            wechat.run(self.ctx("wechat"))

    def test_account_options_passed(self):
        self.feed(WX_ARTICLES)
        wechat.run(self.ctx("wechat", accounts=["Kevin策略研究", "另一家"], count=2, scroll=5))
        self.assertEqual(self.calls[0]["accounts"], ["Kevin策略研究", "另一家"])
        self.assertEqual(self.calls[0]["count"], 2)
        self.assertEqual(self.calls[0]["scroll"], 5)
        self.assertFalse(self.calls[0]["feed"])

    def test_feed_mode(self):
        self.feed([{"account": "公众号 feed", "title": "feed 文章", "url": None,
                    "paragraphs": ["正文"]}])
        wechat.run(self.ctx("wechat", feed=True))
        self.assertTrue(self.calls[0]["feed"])

    def test_first_run_primes(self):
        self.feed(WX_ARTICLES)
        self.assertEqual(wechat.run(self.ctx("wechat", accounts=["Kevin策略研究"])), [])
        state = self.state()
        self.assertTrue(state["primed"])
        self.assertEqual(len(state["seen"]), 2)
        self.assertFalse((self.root / "articles").exists())

    def test_new_article_written_per_file(self):
        self.feed(WX_ARTICLES)
        wechat.run(self.ctx("wechat", accounts=["Kevin策略研究"]))
        self.feed([{"account": "Kevin策略研究", "title": "文章三",
                    "url": "https://mp.weixin.qq.com/s/NewToken123", "paragraphs": ["新正文"]}])
        files = wechat.run(self.ctx("wechat", accounts=["Kevin策略研究"]))
        self.assertEqual(len(files), 1)
        body = files[0].read_text()
        self.assertIn("# 文章三", body)
        self.assertIn("新正文", body)
        self.assertIn("NewToken123", self.state()["seen"])

    def test_cli_uses_config_by_default(self):
        self.feed(WX_ARTICLES)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = wechat.main([], config={"accounts": ["Kevin策略研究"], "count": 2, "scroll": 3})
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[0]["accounts"], ["Kevin策略研究"])
        self.assertEqual(self.calls[0]["count"], 2)
        self.assertEqual(self.calls[0]["scroll"], 3)

    def test_cli_overrides_config_accounts(self):
        self.feed(WX_ARTICLES)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            wechat.main(["--account", "另一家", "--count", "1"],
                        config={"accounts": ["Kevin策略研究"], "count": 3})
        self.assertEqual(self.calls[0]["accounts"], ["另一家"])
        self.assertEqual(self.calls[0]["count"], 1)

    def test_article_ids(self):
        token = wechat.article_id("a", {"url": "https://mp.weixin.qq.com/s/AbCdEfGh"})
        self.assertEqual(token, "AbCdEfGh")
        query = wechat.article_id("a", {"url": "https://mp.weixin.qq.com/s?__biz=x&mid=22&idx=1&sn=ff"})
        self.assertEqual(query, "22_1")
        hashed = wechat.article_id("a", {"title": "同名", "url": ""})
        self.assertTrue(hashed.startswith("t"))

    def test_reader_failure_propagates(self):
        wechat.read_articles = lambda **k: (_ for _ in ()).throw(
            RuntimeError("WeChat shell window not found"))
        with self.assertRaises(RuntimeError):
            wechat.run(self.ctx("wechat", accounts=["x"]))


if __name__ == "__main__":
    unittest.main()
