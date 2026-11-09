"""源模块表：config.toml 里的 `type` → 模块。

每个模块暴露 `run(ctx) -> list[Path]`，产物写进 ctx.outdir，返回写出的文件。
新增平台 = 加一个模块文件并在 MODULES 里登记，主流程不改。
"""
from . import wechat, x, youtube

MODULES = {
    "youtube": youtube,
    "x": x,
    "wechat": wechat,
}
