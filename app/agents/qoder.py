"""Qoder 适配器：账号快照（qoder-switch.sh，优先用 ~/.qoder/ 安装原件）。

签到路径：目前仅切换刷新登录态（未发现独立领取接口；可通过
data/config.json 的 checkin_command 钩子接入真实签到 API）。
"""
from __future__ import annotations

import json
import re
import shutil

from app.agents import register
from app.agents.base import AgentAdapter
from app.core import paths, proc

QD_STATUS_FILE = paths.HOME / ".qoder/.qoder-app-status.json"
QD_SNAP_DIR = paths.HOME / ".qoder/account-switcher-snapshots"
QD_SCRIPT = next((p for p in [paths.HOME / ".qoder/qoder-switch.sh",
                              paths.ROOT / "Qoder/qoder-switch.sh"] if p.is_file()), None)


def _qd_read_status():
    try:
        d = json.loads(QD_STATUS_FILE.read_text(encoding="utf-8"))
        uuid = "?"
        m = re.search(r"/users/([0-9a-f-]{36})/", d.get("avatar_url") or "")
        if m:
            uuid = m.group(1)
        return {"loggedIn": bool(d.get("logged_in")), "name": d.get("name") or "?",
                "email": d.get("email") or "?", "uuid": uuid}
    except Exception:
        return None


@register
class QoderAdapter(AgentAdapter):
    id = "qoder"
    label = "Qoder"
    badge = "QD"
    color = "#f59e0b"
    sort = 2
    settle_seconds = 8
    needs_restart = True
    sign_note = "登录态已刷新（应用已重启并恢复该账号）"

    def state(self) -> dict:
        current = _qd_read_status()
        accounts = []
        if QD_SNAP_DIR.is_dir():
            for d in sorted(p for p in QD_SNAP_DIR.iterdir() if p.is_dir()):
                if not (d / "auth.v1.dat").is_file():
                    continue
                try:
                    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
                email, account_name = meta.get("email", "?"), meta.get("accountName", "?")
                # 显示标识：邮箱 > 账号名，均缺失回退快照名
                display = email if email not in ("?", "", None) else (
                    account_name if account_name not in ("?", "", None) else d.name)
                accounts.append({
                    "name": d.name,
                    "display": display,
                    "sub": f"{account_name} <{email}>",
                    "mtime": int((d / "auth.v1.dat").stat().st_mtime),
                    "active": bool(current and current["uuid"] not in ("?", "None")
                                   and meta.get("accountUuid") == current["uuid"]),
                })
        cur_label = None
        if current and current.get("loggedIn"):
            hit = next((a["display"] for a in accounts if a["active"]), None)
            cur_label = hit or f"未保存账号 ({current['name']})"
        return {"current": current, "currentLabel": cur_label, "accounts": accounts,
                "scriptFound": QD_SCRIPT is not None}

    def save(self, name: str, force: bool = False) -> dict:
        return proc.run_shell(QD_SCRIPT, "未找到 qoder-switch.sh", "save", name,
                              force=force, timeout=180)

    def switch(self, name: str) -> dict:
        return proc.run_shell(QD_SCRIPT, "未找到 qoder-switch.sh", "switch", name,
                              timeout=180)

    def delete(self, name: str) -> dict:
        d = QD_SNAP_DIR / name
        if not d.is_dir():
            return {"ok": False, "output": f"无此快照: {name}"}
        try:
            shutil.rmtree(d)
            return {"ok": True, "output": f"已删除快照「{name}」"}
        except OSError as e:
            return {"ok": False, "output": f"删除失败: {e}"}
