"""新 agent 适配器模板（复制本文件为 <agent_id>.py 并实现 TODO 处即可接入）。

接入后无需改动任何其他代码：
  - Web / 桌面端卡片、配置开关、签到编排自动出现
  - 可选声明 Feature 扩展能力（job / action / import 三种通用 UI）
  - 富交互可设 renderer（前端目前内置 "growth-tasks"，可按需增加）

约定：
  - 核心脚本/模块放在仓库根的 <AgentName>/ 目录（单一逻辑源），适配器只做桥接；
  - 动作方法统一返回 {"ok": bool, "output": str}；
  - state() 的 accounts 每项含 name/display/sub/mtime/active，可选 token: True
    标记 API 直连账号（配合 direct_checkin 使用，name 需以 "token:" 前缀）。
"""
from __future__ import annotations

from app.agents import register
from app.agents.base import AgentAdapter, Feature
from app.core import paths, proc

# 例：SCRIPT = paths.HOME / ".yourapp/switch.sh" 或 paths.ROOT / "YourApp/switch.py"


@register
class TemplateAdapter(AgentAdapter):
    id = "template"            # TODO: 唯一 id（同时是配置键）
    label = "Template Agent"   # TODO: 展示名
    badge = "TP"
    color = "#a78bfa"
    sort = 90
    supported_os = ("darwin",)  # 核心脚本支持的系统；win32/linux 可后续补充

    settle_seconds = 5
    needs_restart = True
    sign_note = "登录态已刷新"

    def state(self) -> dict:
        # TODO: 读当前登录账号与本地快照列表
        return {"current": None, "currentLabel": None, "accounts": [],
                "scriptFound": False}

    def save(self, name: str, force: bool = False) -> dict:
        # TODO: 委托核心脚本保存当前登录态
        return proc.run_shell(None, "核心脚本未实现", "save", name, force=force)

    def switch(self, name: str) -> dict:
        return proc.run_shell(None, "核心脚本未实现", "switch", name)

    def delete(self, name: str) -> dict:
        return proc.run_shell(None, "核心脚本未实现", "remove", name)

    # ---- 可选：原生领取 ----
    # def claim(self, account_display, since_ts): return True, "签到成功"

    # ---- 可选：API 直连账号 ----
    # def direct_checkin(self, name): return None

    # ---- 可选：扩展能力 ----
    # def features(self) -> list[Feature]:
    #     return [Feature(id="refresh", label="刷新登录态", kind="action",
    #                     run=lambda payload: proc.run_shell(None, "未实现", "refresh"))]
