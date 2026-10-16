#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多平台自动签到中心 —— FastAPI 后端

聚合 AccountTools 下三套切号器（Workbuddy / Trae / Qoder），提供统一 Web 控制台：

  * 账号切换：所有动作仍调用各平台既有核心脚本（单一逻辑源），本服务只做编排。
  * 签到（真实领取，非"假设自动发放"）：
      - WorkBuddy：切换账号后经 wbipc(wb.request) 通道让应用进程用当前会话
        POST /v2/billing/meter/daily-checkin 代领每日积分（见 wb_checkin.py）。
      - Trae CN：切换账号并重启应用，trae-autocheckin 扩展在应用内执行
        workbench.action.icubeClaimCheckinCredits 自动领取（绕过 mssdk 签名墙）。
      - Qoder：仅切换刷新登录态（如有领取接口待后续接入）。
      - 各平台亦可配置自定义 checkin_command 钩子优先执行。
  * 自动签到：内置调度循环，到点（或补签）自动对启用平台执行全账号签到。

仅监听 127.0.0.1，不对外网/局域网暴露。

用法:
  .venv/bin/python main.py [--port 8800] [--no-open]
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# --------------------------------------------------------------------------
# 路径与常量
# --------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # AccountTools/
DATA_DIR = HERE / "data"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "logs.jsonl"
STATIC_DIR = HERE / "static"
HOME = Path.home()

NAME_RE = re.compile(r"^[\w一-龥][\w一-龥-]{0,31}$", re.UNICODE)

DEFAULT_CONFIG = {
    "auto_enabled": True,          # 自动签到总开关
    "auto_resign": True,           # 自动补签：错过时间点后在当天补跑
    "auto_time": "05:20",          # 每天自动签到时间 HH:MM
    "restore_after_checkin": True, # 签到完成后切回原来的账号
    "last_auto_date": "",          # 最近一次自动签到日期（内部状态）
    "platforms": {
        "workbuddy": {"enabled": True, "checkin_command": ""},
        "trae":      {"enabled": True, "checkin_command": ""},
        "qoder":     {"enabled": True, "checkin_command": ""},
    },
}

_config_lock = threading.Lock()
_checkin_lock = threading.Lock()   # 同一时刻只允许一个签到/切换流程


def _load_dotenv() -> None:
    """极简 .env 加载(标准库实现,无第三方依赖)。

    依序查找仓库根(../)与本目录(SignCenter/)的 .env,注入 os.environ;
    已存在的环境变量不覆盖。.env 仅放本地偏好(见 .env.example),
    本项目所有账号凭据都不入库、也不经 .env 传递。
    """
    for cand in (ROOT / ".env", HERE / ".env"):
        if not cand.is_file():
            continue
        try:
            for raw in cand.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception:
            pass
        break


_load_dotenv()


# --------------------------------------------------------------------------
# 配置与日志存储
# --------------------------------------------------------------------------
def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(user, dict):
            for k, v in user.items():
                if k == "platforms" and isinstance(v, dict):
                    for pid, pv in v.items():
                        if pid in cfg["platforms"] and isinstance(pv, dict):
                            cfg["platforms"][pid].update(pv)
                else:
                    cfg[k] = v
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def append_log(platform: str, ok: bool, msg: str, account: str = "", trigger: str = "manual") -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
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
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_logs(limit: int = 100) -> list:
    if not LOG_PATH.is_file():
        return []
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
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
    """{platform_id: {"done": int, "total_accounts_ok": [...]}} 今日签到成功记录。"""
    today = time.strftime("%Y-%m-%d")
    status: dict[str, dict] = {}
    if not LOG_PATH.is_file():
        return status
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
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


# --------------------------------------------------------------------------
# 平台适配层 —— WorkBuddy
# --------------------------------------------------------------------------
WB_AUTH_FILE = HOME / "Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
WB_SNAP_DIR = HOME / ".workbuddy/account-switcher-snapshots"
WB_SCRIPT = next((p for p in [HOME / ".workbuddy/wb-switch.sh", ROOT / "Workbuddy/wb-switch.sh"] if p.is_file()), None)


def _wb_read_account(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        acc = data.get("account") or {}
        return {"uid": (acc.get("uid") or "?")[:8], "uin": acc.get("uin") or "?"}
    except Exception:
        return None


def wb_state() -> dict:
    current = _wb_read_account(WB_AUTH_FILE) if WB_AUTH_FILE.is_file() else None
    accounts = []
    if WB_SNAP_DIR.is_dir():
        for f in sorted(WB_SNAP_DIR.glob("*.info")):
            if not f.is_file():
                continue
            info = _wb_read_account(f) or {}
            uin, uid = info.get("uin", "?"), info.get("uid", "?")
            # 显示标识：uin（用户号码）> uid，均缺失回退快照名
            display = uin if uin not in ("?", "", None) else (uid if uid not in ("?", "", None) else f.stem)
            accounts.append({
                "name": f.stem,
                "display": display,
                "sub": f"uid {uid} · uin {uin}",
                "mtime": int(f.stat().st_mtime) if f.exists() else 0,
                "active": bool(current and uid != "?" and uid == current.get("uid")),
            })
    cur_label = None
    if current:
        hit = next((a["display"] for a in accounts if a["active"]), None)
        cur_label = hit or f"未保存账号 (uid {current['uid']})"
    return {"current": current, "currentLabel": cur_label, "accounts": accounts,
            "scriptFound": WB_SCRIPT is not None, "needsRestart": False}


def _run_shell_script(script: Path | None, not_found_msg: str, *args, force=False, timeout=120) -> dict:
    if script is None:
        return {"ok": False, "output": not_found_msg}
    env = dict(os.environ)
    if force:
        env["FORCE"] = "1"
    try:
        proc = subprocess.run(["bash", str(script), *args],
                              capture_output=True, text=True, timeout=timeout, env=env)
        return {"ok": proc.returncode == 0,
                "output": (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"脚本执行超时({timeout}s)"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "output": f"执行失败: {e}"}


def wb_save(name: str, force=False):
    return _run_shell_script(WB_SCRIPT, "未找到 wb-switch.sh", "save", name, force=force)


def wb_switch(name: str):
    return _run_shell_script(WB_SCRIPT, "未找到 wb-switch.sh", "switch", name)


def wb_delete(name: str):
    f = WB_SNAP_DIR / f"{name}.info"
    if not f.is_file():
        return {"ok": False, "output": f"无此快照: {name}"}
    try:
        f.unlink()
        return {"ok": True, "output": f"已删除快照「{name}」"}
    except OSError as e:
        return {"ok": False, "output": f"删除失败: {e}"}


# --------------------------------------------------------------------------
# 平台适配层 —— Qoder
# --------------------------------------------------------------------------
QD_STATUS_FILE = HOME / ".qoder/.qoder-app-status.json"
QD_SNAP_DIR = HOME / ".qoder/account-switcher-snapshots"
QD_SCRIPT = next((p for p in [HOME / ".qoder/qoder-switch.sh", ROOT / "Qoder/qoder-switch.sh"] if p.is_file()), None)


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


def qd_state() -> dict:
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
            display = email if email not in ("?", "", None) else (account_name if account_name not in ("?", "", None) else d.name)
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
            "scriptFound": QD_SCRIPT is not None, "needsRestart": True}


def qd_save(name: str, force=False):
    return _run_shell_script(QD_SCRIPT, "未找到 qoder-switch.sh", "save", name, force=force, timeout=180)


def qd_switch(name: str):
    return _run_shell_script(QD_SCRIPT, "未找到 qoder-switch.sh", "switch", name, timeout=180)


def qd_delete(name: str):
    d = QD_SNAP_DIR / name
    if not d.is_dir():
        return {"ok": False, "output": f"无此快照: {name}"}
    try:
        shutil.rmtree(d)
        return {"ok": True, "output": f"已删除快照「{name}」"}
    except OSError as e:
        return {"ok": False, "output": f"删除失败: {e}"}


# --------------------------------------------------------------------------
# 平台适配层 —— Trae（直接复用 Trae/trae_switch.py，单一逻辑源）
# --------------------------------------------------------------------------
TRAE_MODULE_PATH = ROOT / "Trae/trae_switch.py"
TRAE_PROFILE = os.environ.get("TRAE_PROFILE", "cn")
_ts = None
if TRAE_MODULE_PATH.is_file():
    _spec = importlib.util.spec_from_file_location("trae_switch", TRAE_MODULE_PATH)
    _ts = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_ts)


def trae_state() -> dict:
    if _ts is None:
        return {"current": None, "currentLabel": None, "accounts": [],
                "scriptFound": False, "needsRestart": True}
    try:
        st = _ts.build_state(TRAE_PROFILE)
    except Exception as e:  # noqa: BLE001
        return {"current": None, "currentLabel": None, "accounts": [],
                "scriptFound": False, "needsRestart": True, "error": str(e)}
    accounts = [{
        "name": sn["name"],
        # 显示标识：邮箱 > 手机号 > userId，均缺失回退快照名
        "display": sn.get("email") or sn.get("phone") or sn.get("userId") or sn["name"],
        "sub": f"{sn.get('username') or '(未解析)'} · {sn.get('plan') or '—'} · uid {sn.get('userId', '?')}",
        "mtime": sn.get("savedAt", 0),
        "active": bool(sn.get("active")),
    } for sn in st.get("snapshots", [])]
    cur = st.get("current")
    cur_label = None
    if cur and cur.get("userId"):
        hit = next((a["display"] for a in accounts if a["active"]), None)
        cur_label = hit or f"未保存账号 ({cur.get('email') or cur.get('phone') or cur.get('userId')})"
    return {"current": cur, "currentLabel": cur_label, "accounts": accounts,
            "scriptFound": True, "needsRestart": True, "running": st.get("running"),
            "profileLabel": st.get("profileLabel", "Trae CN")}


def _trae_cli(*args, timeout=240) -> dict:
    if _ts is None:
        return {"ok": False, "output": "未找到 Trae/trae_switch.py"}
    try:
        proc = subprocess.run(
            [sys.executable, str(TRAE_MODULE_PATH), "--profile", TRAE_PROFILE, "--json", *args],
            capture_output=True, text=True, timeout=timeout)
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            return {"ok": proc.returncode == 0,
                    "output": (proc.stdout + proc.stderr).strip() or f"exit={proc.returncode}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"脚本执行超时({timeout}s)"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "output": f"执行失败: {e}"}


def trae_save(name: str, force=False):
    return _trae_cli("save", name, *( ["--force"] if force else [] ))


def trae_switch(name: str):
    return _trae_cli("switch", name)


def trae_delete(name: str):
    return _trae_cli("delete", name)


# --------------------------------------------------------------------------
# 平台适配层 —— WorkBuddy 真实签到（经 wbipc 让应用代领积分）
# --------------------------------------------------------------------------
WB_CHECKIN = HERE / "wb_checkin.py"
TRAE_CHECKIN_LOG = HOME / ".trae-cn" / "autocheckin.log"


def wb_claim(account_display: str = "", since_ts: int = 0):
    """调 wb_checkin.py claim：应用进程用当前会话向官方接口领取每日积分。"""
    try:
        proc = subprocess.run([sys.executable, str(WB_CHECKIN), "claim"],
                              capture_output=True, text=True, timeout=150)
        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
        r = json.loads(line)
    except Exception as e:  # noqa: BLE001
        return False, f"领取异常: {e}"
    if r.get("ok") and r.get("claimed"):
        return True, f"签到成功，+{r.get('credit')} 积分（连续 {r.get('streak_days')} 天）"
    if r.get("ok") and r.get("already"):
        return True, "今日已签过，无需重复领取"
    return False, f"签到失败: {r.get('msg') or r.get('error') or '未知错误'}"


def trae_claim_wait(since_ts: int, max_wait: int = 45):
    """等 trae-autocheckin 扩展在本轮重启后写入领取结果日志。"""
    deadline = time.time() + max_wait
    # 扩展日志用 new Date().toISOString()（UTC），比较前缀需用同一时区
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since_ts))
    while time.time() < deadline:
        try:
            lines = TRAE_CHECKIN_LOG.read_text(encoding="utf-8",
                                               errors="replace").splitlines()[-30:]
        except Exception:
            return None, None
        for ln in reversed(lines):
            if since_ts and ln[:19] < since_iso:
                break  # 已翻到本轮重启之前的旧日志
            if "claim executed" in ln:
                return True, "扩展已执行领取命令（应用内自动领取完成）"
            if "no claim needed" in ln:
                return True, "今日已签或未开放（扩展确认无需领取）"
            if "claim error" in ln:
                return False, f"扩展领取报错: {ln.split('claim error')[1][:80]}"
        time.sleep(3)
    return None, None


# --------------------------------------------------------------------------
# 平台注册表
# --------------------------------------------------------------------------
PLATFORMS = {
    "workbuddy": {
        "label": "WorkBuddy", "badge": "WB", "color": "#6366f1",
        "state": wb_state, "save": wb_save, "switch": wb_switch, "delete": wb_delete,
        "settle_seconds": 3,
        "claim": wb_claim,
        "sign_note": "已通过应用代领每日积分（wbipc 通道，带官方鉴权头）",
    },
    "trae": {
        "label": "Trae CN", "badge": "TR", "color": "#0ea5e9",
        "state": trae_state, "save": trae_save, "switch": trae_switch, "delete": trae_delete,
        "settle_seconds": 8,
        "always_relaunch": True,  # 扩展只在应用启动时激活：同号也要重启以触发领取
        "claim": lambda acc, since: trae_claim_wait(since),
        "sign_note": "应用已重启，trae-autocheckin 扩展会自动领取签到积分（日志见 ~/.trae-cn/autocheckin.log）",
    },
    "qoder": {
        "label": "Qoder", "badge": "QD", "color": "#f59e0b",
        "state": qd_state, "save": qd_save, "switch": qd_switch, "delete": qd_delete,
        "settle_seconds": 8,
        "sign_note": "登录态已刷新（应用已重启并恢复该账号）",
    },
}


# --------------------------------------------------------------------------
# 签到编排
# --------------------------------------------------------------------------
def _run_hook(cmd: str, platform: str, account: str) -> tuple[bool, str]:
    """执行用户自定义签到钩子（可选），环境变量注入平台与账号名。"""
    env = dict(os.environ, SIGN_PLATFORM=platform, SIGN_ACCOUNT=account)
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=60, env=env)
        out = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, out or f"exit={proc.returncode}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def platform_checkin(pid: str, only_account: str | None = None, trigger: str = "manual") -> dict:
    """对一个平台执行签到：逐个切换账号刷新登录态，最后按需切回原账号。"""
    pf = PLATFORMS[pid]
    cfg = load_config()
    pcfg = cfg["platforms"].get(pid, {})
    results = []

    with _checkin_lock:
        state = pf["state"]()
        accounts = state["accounts"]
        if not accounts:
            msg = "暂无账号快照，请先在应用内登录并用「添加账号」保存"
            append_log(pid, False, msg, trigger=trigger)
            return {"ok": False, "output": msg, "results": results}

        if only_account:
            targets = [a["name"] for a in accounts if a["name"] == only_account]
            if not targets:
                msg = f"无此账号快照: {only_account}"
                append_log(pid, False, msg, account=only_account, trigger=trigger)
                return {"ok": False, "output": msg, "results": results}
        else:
            targets = [a["name"] for a in accounts]

        original = next((a["name"] for a in accounts if a["active"]), None)
        disp = {a["name"]: a.get("display") or a["name"] for a in accounts}

        for name in targets:
            try:
                cur = pf["state"]()
                active = next((a["name"] for a in cur["accounts"] if a["active"]), None)
                switched = False
                t_switch = int(time.time())
                if active != name or pf.get("always_relaunch"):
                    r = pf["switch"](name)
                    if not r.get("ok"):
                        out = r.get("output", "切换失败")
                        append_log(pid, False, f"[签到] {disp[name]}: 切换失败 — {out}",
                                   account=disp[name], trigger=trigger)
                        results.append({"account": disp[name], "ok": False, "msg": out})
                        continue
                    switched = True
                    time.sleep(pf["settle_seconds"])

                hook = (pcfg.get("checkin_command") or "").strip()
                claim_fn = pf.get("claim")
                if hook:
                    ok, out = _run_hook(hook, pid, name)
                    msg = f"[签到] {disp[name]}: 钩子{'成功' if ok else '失败'} — {out[:200]}"
                    append_log(pid, ok, msg, account=disp[name], trigger=trigger)
                    results.append({"account": disp[name], "ok": ok, "msg": out[:200]})
                elif claim_fn:
                    # 平台原生领取：WorkBuddy 经 wbipc 代领；Trae 由扩展领取并确认日志
                    ok, out = claim_fn(disp[name], t_switch)
                    if ok is None:  # Trae 扩展异步领取，尚未出结果
                        msg = f"[签到] {disp[name]}: 已切换并重启，扩展领取中（异步，稍后见日志）"
                        append_log(pid, True, msg, account=disp[name], trigger=trigger)
                        results.append({"account": disp[name], "ok": True, "msg": "扩展领取中"})
                    else:
                        append_log(pid, ok, f"[签到] {disp[name]}: {out}",
                                  account=disp[name], trigger=trigger)
                        results.append({"account": disp[name], "ok": ok, "msg": out})
                else:
                    note = pf["sign_note"] if switched else "当前已是该账号，登录态在线有效"
                    append_log(pid, True, f"[签到] {disp[name]}: {note}",
                               account=disp[name], trigger=trigger)
                    results.append({"account": disp[name], "ok": True, "msg": note})
            except Exception as e:  # noqa: BLE001
                append_log(pid, False, f"[签到] {disp.get(name, name)}: 异常 — {type(e).__name__}: {e}",
                           account=disp.get(name, name), trigger=trigger)
                results.append({"account": disp.get(name, name), "ok": False, "msg": str(e)})

        # 切回原来的账号，避免影响用户当前使用
        if cfg.get("restore_after_checkin") and original and len(targets) > 1:
            try:
                cur = pf["state"]()
                active = next((a["name"] for a in cur["accounts"] if a["active"]), None)
                if active != original:
                    pf["switch"](original)
                    append_log(pid, True, f"[签到] 已切回签到前账号「{disp[original]}」",
                               account=disp[original], trigger=trigger)
            except Exception as e:  # noqa: BLE001
                append_log(pid, False, f"[签到] 切回原账号失败: {e}", trigger=trigger)

    ok_all = bool(results) and all(r["ok"] for r in results)
    done = sum(1 for r in results if r["ok"])
    return {"ok": ok_all, "output": f"{pf['label']}: {done}/{len(results)} 个账号签到成功",
            "results": results}


def checkin_all(trigger: str = "manual") -> dict:
    cfg = load_config()
    summary = []
    for pid, pf in PLATFORMS.items():
        if not cfg["platforms"].get(pid, {}).get("enabled", True):
            continue
        res = platform_checkin(pid, trigger=trigger)
        summary.append({"platform": pid, "label": pf["label"],
                        "ok": res["ok"], "output": res["output"]})
    return {"ok": all(s["ok"] for s in summary) if summary else False, "summary": summary}


# --------------------------------------------------------------------------
# 自动签到调度
# --------------------------------------------------------------------------
async def scheduler_loop():
    while True:
        try:
            cfg = load_config()
            if cfg.get("auto_enabled"):
                today = time.strftime("%Y-%m-%d")
                hh, mm = (cfg.get("auto_time") or "05:20").split(":")
                target = int(hh) * 60 + int(mm)
                now = time.localtime()
                now_min = now.tm_hour * 60 + now.tm_min
                due = now_min >= target if cfg.get("auto_resign") else target <= now_min <= target + 1
                if due and cfg.get("last_auto_date") != today:
                    cfg["last_auto_date"] = today
                    save_config(cfg)
                    append_log("system", True, f"[自动签到] 到点 {cfg.get('auto_time')}，开始全平台签到", trigger="auto")
                    await asyncio.to_thread(checkin_all, "auto")
        except Exception as e:  # noqa: BLE001
            append_log("system", False, f"[自动签到] 调度异常: {type(e).__name__}: {e}", trigger="auto")
        await asyncio.sleep(30)


# --------------------------------------------------------------------------
# FastAPI 应用
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="多平台自动签到中心", lifespan=lifespan)


class SaveBody(BaseModel):
    name: str
    force: bool = False


class NameBody(BaseModel):
    name: str


class CheckinBody(BaseModel):
    name: str | None = None


class ToggleBody(BaseModel):
    enabled: bool


class SettingsBody(BaseModel):
    auto_enabled: bool | None = None
    auto_resign: bool | None = None
    auto_time: str | None = None
    restore_after_checkin: bool | None = None


def _valid_name(name: str) -> bool:
    return isinstance(name, str) and bool(NAME_RE.match(name))


@app.get("/")
def index():
    page = STATIC_DIR / "index.html"
    if page.is_file():
        return FileResponse(page, headers={"Cache-Control": "no-store"})
    return JSONResponse({"ok": False, "error": "index.html not found"}, 404)


@app.get("/favicon.svg")
@app.get("/favicon.ico")
def favicon():
    icon = STATIC_DIR / "favicon.svg"
    if icon.is_file():
        return FileResponse(icon, media_type="image/svg+xml",
                            headers={"Cache-Control": "no-store"})
    return JSONResponse({"ok": False, "error": "favicon not found"}, 404)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/overview")
def overview():
    cfg = load_config()
    today = today_platform_status()
    platforms = []
    total_done = total_accounts = 0
    enabled_count = 0
    for pid, pf in PLATFORMS.items():
        pcfg = cfg["platforms"].get(pid, {})
        enabled = pcfg.get("enabled", True)
        try:
            st = pf["state"]()
        except Exception as e:  # noqa: BLE001
            st = {"current": None, "currentLabel": None, "accounts": [], "error": str(e)}
        signed = today.get(pid, {"done": 0, "accounts": []})
        n_acc = len(st["accounts"])
        # 日志中记录的是显示名（邮箱/号码/uid），映射回快照名再与账号列表比对
        disp2name = {a.get("display"): a["name"] for a in st["accounts"] if a.get("display")}
        signed_names = sorted({disp2name.get(x, x) for x in signed["accounts"]})
        done = min(len(signed_names), n_acc) if n_acc else 0
        if enabled:
            enabled_count += 1
            total_done += done
            total_accounts += n_acc
        platforms.append({
            "id": pid, "label": pf["label"], "badge": pf["badge"], "color": pf["color"],
            "enabled": enabled,
            "needsRestart": st.get("needsRestart", False),
            "scriptFound": st.get("scriptFound", False),
            "currentLabel": st.get("currentLabel"),
            "accounts": st.get("accounts", []),
            "signedToday": done,
            "signedAccounts": signed_names,
            "allSigned": bool(n_acc) and done >= n_acc,
        })
    return {
        "platforms": platforms,
        "progress": {"done": total_done, "total": total_accounts, "enabledPlatforms": enabled_count},
        "auto": {"enabled": cfg.get("auto_enabled"), "time": cfg.get("auto_time"),
                 "resign": cfg.get("auto_resign"),
                 "restore": cfg.get("restore_after_checkin")},
    }


@app.post("/api/platform/{pid}/toggle")
def toggle(pid: str, body: ToggleBody):
    if pid not in PLATFORMS:
        return JSONResponse({"ok": False, "output": "未知平台"}, 404)
    cfg = load_config()
    cfg["platforms"].setdefault(pid, {})["enabled"] = body.enabled
    save_config(cfg)
    return {"ok": True, "output": f"{'已启用' if body.enabled else '已停用'} {PLATFORMS[pid]['label']}"}


@app.post("/api/platform/{pid}/save")
def save(pid: str, body: SaveBody):
    if pid not in PLATFORMS:
        return JSONResponse({"ok": False, "output": "未知平台"}, 404)
    if not _valid_name(body.name):
        return {"ok": False, "output": "无效的快照名：仅允许中文/字母/数字/下划线/连字符，1~32 位"}
    return PLATFORMS[pid]["save"](body.name, force=body.force)


@app.post("/api/platform/{pid}/switch")
def switch(pid: str, body: NameBody):
    if pid not in PLATFORMS:
        return JSONResponse({"ok": False, "output": "未知平台"}, 404)
    if not _valid_name(body.name):
        return {"ok": False, "output": "无效的快照名"}
    r = PLATFORMS[pid]["switch"](body.name)
    append_log(pid, r.get("ok", False), f"[切换] {body.name}: {r.get('output', '')[:200]}",
               account=body.name, trigger="switch")
    return r


@app.post("/api/platform/{pid}/delete")
def delete(pid: str, body: NameBody):
    if pid not in PLATFORMS:
        return JSONResponse({"ok": False, "output": "未知平台"}, 404)
    if not _valid_name(body.name):
        return {"ok": False, "output": "无效的快照名"}
    return PLATFORMS[pid]["delete"](body.name)


@app.post("/api/platform/{pid}/checkin")
def checkin(pid: str, body: CheckinBody):
    if pid not in PLATFORMS:
        return JSONResponse({"ok": False, "output": "未知平台"}, 404)
    return platform_checkin(pid, only_account=body.name or None, trigger="manual")


@app.post("/api/checkin/all")
def checkin_all_api():
    return checkin_all(trigger="manual")


@app.post("/api/settings")
def settings(body: SettingsBody):
    cfg = load_config()
    if body.auto_enabled is not None:
        cfg["auto_enabled"] = body.auto_enabled
    if body.auto_resign is not None:
        cfg["auto_resign"] = body.auto_resign
    if body.restore_after_checkin is not None:
        cfg["restore_after_checkin"] = body.restore_after_checkin
    if body.auto_time is not None:
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", body.auto_time):
            return {"ok": False, "output": "时间格式应为 HH:MM（24 小时制）"}
        cfg["auto_time"] = body.auto_time
    save_config(cfg)
    return {"ok": True, "output": "设置已保存", "auto": {
        "enabled": cfg["auto_enabled"], "time": cfg["auto_time"],
        "resign": cfg["auto_resign"], "restore": cfg["restore_after_checkin"]}}


@app.get("/api/logs")
def logs(limit: int = 100):
    return {"logs": read_logs(max(1, min(limit, 500)))}


def main():
    ap = argparse.ArgumentParser(description="多平台自动签到中心")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("SIGNCENTER_PORT") or 8800))
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    print(f"多平台自动签到中心已启动: {url}  (Ctrl+C 退出)")
    print(f"  WorkBuddy 脚本: {WB_SCRIPT or '未找到'}")
    print(f"  Qoder 脚本:     {QD_SCRIPT or '未找到'}")
    print(f"  Trae 模块:      {TRAE_MODULE_PATH if _ts else '未找到'}")
    print(f"  数据目录:       {DATA_DIR}")
    if not args.no_open:
        webbrowser.open(url)

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
