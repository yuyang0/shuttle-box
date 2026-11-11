"""模块共用的上下文：到期判断、产物落盘、源私有状态。

模块只暴露 `run(ctx) -> list[Path]`；平台细节都在模块里，主流程不认识任何平台。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from shuttle_client import Client, desk_call            # noqa: E402

DEFAULT_SEEN_KEEP_DAYS = 60
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
DURATION = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.I)


class ConfigError(RuntimeError):
    """配置写错就直接退出，别带着半份配置跑。"""


def parse_duration(text: str | int) -> timedelta:
    """`30m` / `6h` / `1d` / `1w`，纯数字按秒。"""
    if isinstance(text, int):
        return timedelta(seconds=text)
    m = DURATION.match(str(text))
    if not m:
        raise ConfigError(f"every 无法解析：{text!r}（示例：30m、6h、1d、1w）")
    unit = (m.group(2) or "s").lower()
    return timedelta(seconds=int(m.group(1)) * UNIT_SECONDS[unit])


def parse_time(value: str | None) -> datetime | None:
    """读 last_success.json；没时区的时间按本机时区处理，免得和 now 比较时炸掉。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ConfigError(f"时间戳无法解析：{value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def now_local() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_stamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_seen(state: dict, keep_days: int) -> dict[str, str]:
    """seen 是 {条目 ID: 时间}；旧格式（ID 列表）按「刚看到」升级，再丢掉 keep_days 天前的记录。"""
    raw = state.get("seen")
    if isinstance(raw, list):
        seen = {str(iid): now_iso() for iid in raw if iid}
    elif isinstance(raw, dict):
        seen = {str(k): str(v) for k, v in raw.items()}
    else:
        seen = {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    kept = {}
    for iid, stamp in seen.items():
        parsed = parse_stamp(stamp)
        if parsed is not None and parsed < cutoff:
            continue
        kept[iid] = stamp
    return kept


def refresh_seen(ctx: "Ctx", state: dict) -> None:
    """按 seen_keep_days 裁剪已读记录；有变化才落盘，免得没产出时白写一次。"""
    before = state.get("seen")
    state["seen"] = load_seen(state, int(ctx.config.get("seen_keep_days", DEFAULT_SEEN_KEEP_DAYS)))
    if state["seen"] != before:
        ctx.save_state(state)


@dataclass
class Ctx:
    """一次源运行的上下文：配置、输出目录、上次成功时间。"""

    name: str
    config: dict
    outdir: Path
    last_success: datetime | None
    force: bool = False
    now: datetime = field(default_factory=now_local)

    # ── 调度 ──
    def due(self) -> bool:
        """距上次成功抓到内容是否已满 every；从未成功、或 --force 一律到期。"""
        if self.force or self.last_success is None:
            return True
        return self.now - self.last_success >= parse_duration(self.config.get("every", "1d"))

    def due_at(self) -> datetime | None:
        if self.last_success is None:
            return None
        return self.last_success + parse_duration(self.config.get("every", "1d"))

    # ── 产物 ──
    def write(self, relpath: str, text: str) -> Path | None:
        """写一个产物；空内容不落盘，返回 None（不留空文件）。"""
        if not text or not text.strip():
            return None
        path = self.outdir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_json(self, relpath: str, payload) -> Path | None:
        return self.write(relpath, json.dumps(payload, ensure_ascii=False, indent=1))

    # ── 源私有状态 ──
    def state(self) -> dict:
        path = self.outdir / "state.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def save_state(self, state: dict) -> None:
        self.outdir.mkdir(parents=True, exist_ok=True)
        tmp = self.outdir / ".state.json.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.outdir / "state.json")


# ───────────────────────── 桌面（shuttle MCP） ─────────────────────────

_CLIENT = None


def desk(*args):
    """经同目录的 shuttle_client 调 MCP 的 `desk` 动词（http → docker → local 自动挑）。"""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = Client("auto")
    return desk_call(_CLIENT, *args)
