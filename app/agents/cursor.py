"""Cursor 适配器：账号快照（cursor-switch.sh，优先用 ~/.cursor/ 安装原件）。

只读解析 Cursor 的 state.vscdb（cursorAuth/* 键）展示当前账号与 token 有效期；
保存/切换/删除动作全部委托 cursor-switch.sh。附带「重启应用 / 本机退出登录」
两个即时动作特性（Feature kind="action" 的示例）。
"""
from __future__ import annotations

import base64
import json
import subprocess
import time

from app.agents import register
from app.agents.base import AgentAdapter, Feature
from app.core import paths, proc

DB = paths.HOME / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
SNAP_DIR = paths.HOME / ".cursor/account-switcher-snapshots"
SCRIPT = next((p for p in [paths.HOME / ".cursor/cursor-switch.sh",
                           paths.ROOT / "Cursor/cursor-switch.sh"] if p.is_file()), None)


def _read_db_rows():
    """只读打开 state.vscdb，返回 cursorAuth/* 与 lastSignedInAuthId 行（应用运行中安全）。"""
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = dict(conn.execute(
                "SELECT key, value FROM ItemTable "
                "WHERE key LIKE 'cursorAuth/%' OR key='glass.lastSignedInAuthId'"))
        finally:
            conn.close()
        return rows
    except Exception:
        return {}


def _jwt_payload(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def _summarize(rows):
    """从凭据行提取展示信息（与脚本 meta_of 一致）。"""
    at = rows.get("cursorAuth/accessToken")
    if not at:
        return None
    pl = _jwt_payload(at) if isinstance(at, str) else {}
    disp = ""
    try:
        prof = rows.get("cursorAuth/cachedScopedProfile") or ""
        if isinstance(prof, str) and prof:
            disp = json.loads(prof).get("displayName") or ""
    except Exception:
        pass
    exp = pl.get("exp")
    return {
        "authId": pl.get("sub") or "",
        "email": rows.get("cursorAuth/cachedEmail") or "",
        "displayName": disp,
        "membership": rows.get("cursorAuth/stripeMembershipType") or "",
        "signUpType": rows.get("cursorAuth/cachedSignUpType") or "",
        "tokenExp": exp,
        "expDays": int((exp - time.time()) // 86400) if exp else None,
    }


def _running() -> bool:
    try:
        return subprocess.run(["pgrep", "-x", "Cursor"],
                              capture_output=True).returncode == 0
    except Exception:
        return False


@register
class CursorAdapter(AgentAdapter):
    id = "cursor"
    label = "Cursor"
    badge = "CU"
    color = "#22c55e"
    sort = 10
    settle_seconds = 6
    needs_restart = True
    sign_note = "登录态已刷新（应用已重启并恢复该账号）"

    def state(self) -> dict:
        current = _summarize(_read_db_rows())
        snapshots = []
        if SNAP_DIR.is_dir():
            for f in sorted(SNAP_DIR.glob("*.json")):
                if not f.is_file():
                    continue
                info = {}
                try:
                    info = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    info = {"name": f.stem, "error": "解析失败"}
                exp = info.get("tokenExp")
                try:
                    mtime = int(f.stat().st_mtime)
                except OSError:
                    mtime = 0
                snapshots.append({
                    "name": info.get("name") or f.stem,
                    # 显示标识：邮箱 > 显示名，均缺失回退快照名
                    "display": info.get("email") or info.get("displayName") or f.stem,
                    "sub": (f"{info.get('membership') or '—'} · "
                            + (f"token {info.get('expDays')} 天内过期" if info.get("expDays") is not None
                               else "token 有效期未知")),
                    "mtime": mtime,
                    "active": bool(current and info.get("authId") == current.get("authId")),
                    "error": info.get("error"),
                })
        cur_label = None
        if current:
            hit = next((a["display"] for a in snapshots if a["active"]), None)
            cur_label = hit or (current.get("email") or current.get("displayName") or "已登录")
        return {"current": current, "currentLabel": cur_label, "accounts": snapshots,
                "scriptFound": SCRIPT is not None, "running": _running()}

    def save(self, name: str, force: bool = False) -> dict:
        return proc.run_shell(SCRIPT, "未找到 cursor-switch.sh（已尝试 ~/.cursor/ 与脚本同目录）",
                              "save", name, force=force)

    def switch(self, name: str) -> dict:
        return proc.run_shell(SCRIPT, "未找到 cursor-switch.sh", "switch", name)

    def delete(self, name: str) -> dict:
        return proc.run_shell(SCRIPT, "未找到 cursor-switch.sh", "remove", name)

    def features(self) -> list[Feature]:
        def restart(_payload):
            return proc.run_shell(SCRIPT, "未找到 cursor-switch.sh", "restart")

        def signout(_payload):
            return proc.run_shell(SCRIPT, "未找到 cursor-switch.sh", "signout-local")

        return [
            Feature(id="restart", label="重启 Cursor", kind="action",
                    confirm="重启 Cursor 应用？（未保存内容请先保存）", run=restart),
            Feature(id="signout", label="本机退出登录", kind="action",
                    confirm="清除本机 Cursor 登录态（不删除已保存的快照）？", run=signout),
        ]
