"""source-watch 的调度与入口行为；不依赖网络、桌面或转写引擎。"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "source_watch.py"
SCRIPTS_DIR = SCRIPT.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from modules import base as mod_base          # noqa: E402
from modules import youtube                   # noqa: E402


def load_entry(root: Path):
    """按 SOURCEWATCH_ROOT 重新加载入口模块（ROOT 在导入时确定）。"""
    os.environ["SOURCEWATCH_ROOT"] = str(root)
    spec = importlib.util.spec_from_file_location(f"source_watch_{id(root)}", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_run(ctx: mod_base.Ctx):
    if not ctx.due():
        return []
    mode = ctx.config.get("mode", "write")
    if mode == "fail":
        raise RuntimeError("boom")
    if mode == "empty":
        path = ctx.outdir / "empty.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        return [path]
    written = ctx.write("out.txt", "hello")
    return [written] if written else []


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.environ.pop, "SOURCEWATCH_ROOT", None)

    def write_config(self, text: str) -> None:
        (self.root / "config.toml").write_text(text, encoding="utf-8")

    def entry(self):
        module = load_entry(self.root)
        module.MODULES["fake"] = type("FakeSource", (), {"run": staticmethod(fake_run)})
        return module

    def run_main(self, module, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = module.main(list(argv))
        return code, out.getvalue()


class ConfigTests(Base):
    def test_duration_units(self):
        self.assertEqual(mod_base.parse_duration("30m"), timedelta(minutes=30))
        self.assertEqual(mod_base.parse_duration("6h"), timedelta(hours=6))
        self.assertEqual(mod_base.parse_duration("1d"), timedelta(days=1))
        self.assertEqual(mod_base.parse_duration("1w"), timedelta(weeks=1))
        self.assertEqual(mod_base.parse_duration(90), timedelta(seconds=90))
        with self.assertRaises(mod_base.ConfigError):
            mod_base.parse_duration("daily")

    def test_defaults_merge_and_validation(self):
        self.write_config(
            "[defaults]\nevery = \"2h\"\n\n"
            "[sources.a]\ntype = \"fake\"\n\n"
            "[sources.b]\ntype = \"fake\"\nevery = \"1d\"\n"
        )
        sources = self.entry().load_config()
        self.assertEqual(sources["a"]["every"], "2h")
        self.assertEqual(sources["b"]["every"], "1d")

    def test_unknown_type_is_config_error(self):
        self.write_config("[sources.a]\ntype = \"nope\"\nevery = \"1d\"\n")
        code = self.entry().main(["list"])
        self.assertEqual(code, 2)


class RunTests(Base):
    def test_first_run_writes_files_and_success(self):
        self.write_config("[sources.alpha]\ntype = \"fake\"\nevery = \"1d\"\n")
        entry = self.entry()
        code, out = self.run_main(entry, "run", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["sources"][0]["status"], "ok")
        self.assertEqual(payload["sources"][0]["outputs"], ["output/alpha/out.txt"])
        self.assertEqual((self.root / "output/alpha/out.txt").read_text(), "hello")
        last = json.loads((self.root / "last_success.json").read_text())
        self.assertIn("alpha", last)
        pending = json.loads((self.root / "pending.json").read_text())
        self.assertEqual(pending, ["output/alpha/out.txt"])
        self.assertEqual(payload["pending"], pending)

    def test_skips_when_not_due_and_updates_nothing(self):
        self.write_config("[sources.alpha]\ntype = \"fake\"\nevery = \"1d\"\n")
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        (self.root / "last_success.json").write_text(json.dumps({"alpha": stamp}))
        entry = self.entry()
        code, out = self.run_main(entry, "run")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "无新条目")
        self.assertEqual(json.loads((self.root / "last_success.json").read_text())["alpha"], stamp)

    def test_force_ignores_frequency(self):
        self.write_config("[sources.alpha]\ntype = \"fake\"\nevery = \"1d\"\n")
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        (self.root / "last_success.json").write_text(json.dumps({"alpha": stamp}))
        entry = self.entry()
        code, out = self.run_main(entry, "run", "--force", "--json")
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload["sources"][0]["status"], "ok")

    def test_empty_file_does_not_count_as_output(self):
        self.write_config("[sources.alpha]\ntype = \"fake\"\nmode = \"empty\"\nevery = \"1d\"\n")
        entry = self.entry()
        code, out = self.run_main(entry, "run", "--json")
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload["sources"][0]["status"], "empty")
        self.assertEqual(payload["sources"][0]["outputs"], [])
        last = (self.root / "last_success.json")
        self.assertEqual(json.loads(last.read_text()) if last.exists() else {}, {})

    def test_error_isolated_and_exit_code(self):
        self.write_config(
            "[sources.bad]\ntype = \"fake\"\nmode = \"fail\"\nevery = \"1d\"\n\n"
            "[sources.good]\ntype = \"fake\"\nevery = \"1d\"\n"
        )
        entry = self.entry()
        code, out = self.run_main(entry, "run", "--json")
        payload = json.loads(out)
        self.assertEqual(code, 1)
        status = {row["name"]: row["status"] for row in payload["sources"]}
        self.assertEqual(status, {"bad": "error", "good": "ok"})
        self.assertIn("good", json.loads((self.root / "last_success.json").read_text()))

    def test_only_limits_to_one_source(self):
        self.write_config(
            "[sources.a]\ntype = \"fake\"\nevery = \"1d\"\n\n"
            "[sources.b]\ntype = \"fake\"\nevery = \"1d\"\n"
        )
        entry = self.entry()
        code, out = self.run_main(entry, "run", "--only", "a", "--json")
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual([row["name"] for row in payload["sources"]], ["a"])
        self.assertFalse((self.root / "output/b").exists())

    def test_unknown_only_exits_two(self):
        self.write_config("[sources.a]\ntype = \"fake\"\nevery = \"1d\"\n")
        entry = self.entry()
        code, _ = self.run_main(entry, "run", "--only", "zzz")
        self.assertEqual(code, 2)

    def test_yt_subcommand_routes_to_youtube_module(self):
        self.write_config("[sources.a]\ntype = \"fake\"\nevery = \"1d\"\n")
        entry = self.entry()
        module = entry.MODULES["youtube"]
        self.addCleanup(setattr, module, "channel_id", module.channel_id)
        self.addCleanup(setattr, module, "list_items", module.list_items)
        module.channel_id = lambda target: "UC" + "a" * 22
        module.list_items = lambda uid, limit=15: [
            {"id": "v1", "title": "T", "published": "", "url": "https://youtu.be/v1"}]
        code, out = self.run_main(entry, "yt", "--channel", "@x")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["id"], "v1")

    def test_yt_subcommand_reads_config_section(self):
        self.write_config(
            "[sources.yt]\ntype = \"youtube\"\nevery = \"1d\"\n"
            "channels = [\"UCaaaaaaaaaaaaaaaaaaaaaa\"]\n")
        entry = self.entry()
        module = entry.MODULES["youtube"]
        self.addCleanup(setattr, module, "channel_id", module.channel_id)
        self.addCleanup(setattr, module, "list_items", module.list_items)
        module.channel_id = lambda target: target
        module.list_items = lambda uid, limit=15: [
            {"id": "v1", "title": "T", "published": "", "url": "https://youtu.be/v1"}]
        code, out = self.run_main(entry, "yt")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["id"], "v1")

    def test_pending_can_be_listed_and_cleared(self):
        self.write_config("[sources.alpha]\ntype = \"fake\"\nevery = \"1d\"\n")
        entry = self.entry()
        self.run_main(entry, "run")
        code, out = self.run_main(entry, "pending", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), ["output/alpha/out.txt"])
        code, out = self.run_main(entry, "pending", "--clear")
        self.assertEqual(code, 0)
        self.assertIn("已清空 1", out)
        self.assertEqual(json.loads((self.root / "pending.json").read_text()), [])

    def test_reset_clears_state_only(self):
        self.write_config("[sources.a]\ntype = \"fake\"\nevery = \"1d\"\n")
        state = self.root / "output/a/state.json"
        state.parent.mkdir(parents=True)
        state.write_text("{}")
        entry = self.entry()
        code, out = self.run_main(entry, "reset", "a")
        self.assertEqual(code, 0)
        self.assertFalse(state.exists())
        self.assertIn("产物文件与 last_success.json 不在清理范围内", out)


class YouTubeModuleTests(unittest.TestCase):
    RSS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <title>频道</title>
  <entry><yt:videoId>vid1</yt:videoId><title>第一条</title>
    <published>2026-11-01T00:00:00+00:00</published></entry>
  <entry><yt:videoId>vid2</yt:videoId><title>第二条</title>
    <published>2026-11-02T00:00:00+00:00</published></entry>
</feed>"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.saved = {"fetch_text": youtube.fetch_text, "item_meta": youtube.item_meta,
                      "transcribe": youtube.transcribe, "channel_id": youtube.channel_id,
                      "list_items": youtube.list_items}
        for name, fn in self.saved.items():
            setattr(youtube, name, fn)

    def tearDown(self):
        for name, fn in self.saved.items():
            setattr(youtube, name, fn)

    def ctx(self, **cfg):
        config = {"every": "1d", "channels": ["UC" + "a" * 22], **cfg}
        return mod_base.Ctx(name="yt", config=config, outdir=self.root,
                            last_success=None, force=False)

    def test_list_items_parses_rss(self):
        youtube.fetch_text = lambda *a, **k: self.RSS
        items = youtube.list_items("UC" + "a" * 22)
        self.assertEqual([i["id"] for i in items], ["vid1", "vid2"])
        self.assertEqual(items[1]["url"], "https://www.youtube.com/watch?v=vid2")

    def test_list_items_respects_limit(self):
        youtube.fetch_text = lambda *a, **k: self.RSS
        self.assertEqual(len(youtube.list_items("UC" + "a" * 22, limit=1)), 1)

    def test_first_run_primes_and_writes_nothing(self):
        youtube.fetch_text = lambda *a, **k: self.RSS
        ctx = self.ctx()
        self.assertEqual(youtube.run(ctx), [])
        state = json.loads((self.root / "state.json").read_text())
        self.assertTrue(state["primed"])
        self.assertEqual(set(state["seen"]), {"vid1", "vid2"})
        self.assertFalse((self.root / "transcripts").exists())

    def test_second_run_transcribes_only_new_items(self):
        youtube.fetch_text = lambda *a, **k: self.RSS
        ctx = self.ctx()
        youtube.run(ctx)  # prime
        newer = self.RSS.replace("vid2", "vid3").replace("第二条", "第三条")
        youtube.fetch_text = lambda *a, **k: newer
        youtube.item_meta = lambda url: {"duration": 60, "upload_date": "20261103",
                                         "description": ""}
        youtube.transcribe = lambda item, ctx: ctx.write(
            f"transcripts/2026-11-03-{item['id']}.txt", "转写正文")
        files = youtube.run(self.ctx())
        self.assertIn("vid3.json", [p.name for p in files])
        self.assertTrue(any(p.name.endswith("vid3.txt") for p in files))
        state = json.loads((self.root / "state.json").read_text())
        self.assertIn("vid3", state["seen"])
        self.assertEqual(state["queue"], [])

    def test_multiple_channels_aggregate(self):
        youtube.channel_id = lambda target: target
        youtube.list_items = lambda uid, limit=15: [
            {"id": f"{uid}-1", "title": f"频道 {uid}", "published": "",
             "url": f"https://www.youtube.com/watch?v={uid}-1"}]
        self.assertEqual(youtube.run(self.ctx(channels=["A", "B"])), [])
        state = json.loads((self.root / "state.json").read_text())
        self.assertEqual(set(state["seen"]), {"A-1", "B-1"})

    def test_one_failing_channel_does_not_stop_others(self):
        youtube.channel_id = lambda target: target

        def listing(uid, limit=15):
            if uid == "BAD":
                raise RuntimeError("RSS 429")
            return [{"id": f"{uid}-1", "title": "ok", "published": "",
                     "url": f"https://www.youtube.com/watch?v={uid}-1"}]

        youtube.list_items = listing
        self.assertEqual(youtube.run(self.ctx(channels=["BAD", "OK"])), [])
        self.assertEqual(json.loads((self.root / "state.json").read_text())["seen"], ["OK-1"])

    def test_cli_lists_items_as_json(self):
        youtube.channel_id = lambda target: "UC" + "a" * 22
        youtube.list_items = lambda uid, limit=15: [
            {"id": "v1", "title": "第一条", "published": "2026-11-01T00:00:00+00:00",
             "url": "https://www.youtube.com/watch?v=v1"}]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = youtube.main(["--channel", "@x", "--limit", "3"])
        self.assertEqual(code, 0)
        items = json.loads(out.getvalue())
        self.assertEqual([i["id"] for i in items], ["v1"])

    def test_cli_uses_config_channels(self):
        youtube.channel_id = lambda target: "UC" + "a" * 22
        youtube.list_items = lambda uid, limit=15: [
            {"id": "v1", "title": "T", "published": "", "url": "https://youtu.be/v1"}]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = youtube.main([], config={"channels": ["UC" + "a" * 22], "limit": 2})
        self.assertEqual(code, 0)
        self.assertEqual([i["id"] for i in json.loads(out.getvalue())], ["v1"])

    def test_cli_overrides_config_channels(self):
        seen = []
        youtube.channel_id = lambda target: seen.append(target) or "UC" + "a" * 22
        youtube.list_items = lambda uid, limit=15: []
        with contextlib.redirect_stdout(io.StringIO()):
            youtube.main(["--channel", "@cli"], config={"channels": ["@config"]})
        self.assertEqual(seen, ["@cli"])

    def test_cli_limit_overrides_config(self):
        seen = []
        youtube.channel_id = lambda target: "UC" + "a" * 22
        youtube.list_items = lambda uid, limit=15: seen.append(limit) or []
        with contextlib.redirect_stdout(io.StringIO()):
            youtube.main(["--limit", "3"], config={"channels": ["UC" + "a" * 22], "limit": 9})
        self.assertEqual(seen, [3])

    def test_cli_markdown_and_meta(self):
        youtube.channel_id = lambda target: "UC" + "a" * 22
        youtube.list_items = lambda uid, limit=15: [
            {"id": "v1", "title": "第一条", "published": "", "url": "https://youtu.be/v1"}]
        youtube.item_meta = lambda url: {"duration": 600, "upload_date": "20261101",
                                        "description": ""}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = youtube.main(["--channel", "UC" + "a" * 22, "--meta", "--format", "md"])
        body = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("第一条", body)
        self.assertIn("时长 10min", body)

    def test_all_channels_failing_is_an_error(self):
        youtube.channel_id = lambda target: target

        def listing(uid, limit=15):
            raise RuntimeError("RSS 500")

        youtube.list_items = listing
        with self.assertRaises(RuntimeError):
            youtube.run(self.ctx(channels=["A", "B"]))

    def test_long_media_keeps_meta_only(self):
        youtube.fetch_text = lambda *a, **k: self.RSS
        youtube.run(self.ctx())
        newer = self.RSS.replace("vid2", "vid4")
        youtube.fetch_text = lambda *a, **k: newer
        youtube.item_meta = lambda url: {"duration": 7200, "upload_date": "", "description": ""}
        youtube.transcribe = lambda *a, **k: self.fail("长内容不该转写")
        files = youtube.run(self.ctx())
        self.assertEqual([p.name for p in files], ["vid4.json"])
        state = json.loads((self.root / "state.json").read_text())
        self.assertIn("vid4", state["pending_long"])
        self.assertIn("vid4", state["seen"])


if __name__ == "__main__":
    unittest.main()
