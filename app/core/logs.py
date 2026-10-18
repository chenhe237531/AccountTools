"""签到/切换日志（data/logs.jsonl，JSONL 追加写）。"""
from __future__ import annotations

import json
import time

from app.core import paths


def append_log(platform: str, ok: bool, msg: str, account: str = "", trigger: str = "manual") -> None:
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": int(time.time()),
        "time": time.strftime("%m-%d %H:%M:%S"),
        "date": time.strftime("%Y-%m-%d"),
        "platform": platform,
        "account": account,
        "ok": bool(ok),
        "msg": msg,
        "trigger": trigger,
    }
    with open(paths.LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_logs(limit: int = 100) -> list:
    if not paths.LOG_PATH.is_file():
        return []
    try:
        lines = paths.LOG_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out = []
    for line in reversed(lines[-2000:]):
        try:
            out.append(json.loads(line))
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def today_platform_status() -> dict:
    """{agent_id: {"done": n, "accounts": [...]}} 今日签到成功记录。"""
    today = time.strftime("%Y-%m-%d")
    status: dict[str, dict] = {}
    if not paths.LOG_PATH.is_file():
        return status
    try:
        lines = paths.LOG_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return status
    for line in lines:
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("date") != today or not r.get("ok"):
            continue
        if r.get("msg", "").startswith("[签到]") is False and r.get("trigger") == "switch":
            continue
        st = status.setdefault(r.get("platform", ""), {"accounts": set()})
        if r.get("account"):
            st["accounts"].add(r["account"])
    return {pid: {"done": len(v["accounts"]), "accounts": sorted(v["accounts"])}
            for pid, v in status.items() if pid}
