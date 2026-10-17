#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 登录凭证导入器 —— 把外部导出的账号凭证 JSON 变成可直连签到的账号池

背景:
  WorkBuddy(CodeBuddy 系)账号的 access_token / refresh_token 可直接调用
  www.workbuddy.cn 官方签到与成长 API(Bearer 鉴权,本机实测 WAF 放行),
  因此导入凭证后无需切换桌面应用即可完成「每日签到领积分」。

  与 wb-switch.sh 的整文件快照互补:
    * 桌面快照(.info)  → 适合日常主号,由应用管理 token 续期;
    * token 直连账号    → 适合池子账号,凭证来自外部 JSON 导入,签到零打扰。
  两者不要混用同一账号:桌面 daemon 刷新 token 会轮换 refresh_token,
  导致导入的副本失效;池子账号建议只走直连。

存储: ~/.workbuddy/token-accounts.json (权限 600,uid 去重)

输入格式(兼容单对象/数组,字段名大小写与层级自适应):
  [{ uid, nickname, domain, access_token, refresh_token, expiresAt, ... }]
  也支持嵌套 auth_raw.auth.{accessToken, refreshToken, domain, expiresAt}

用法(stdout 最后一行输出单行 JSON,供 SignCenter 解析):
  python3 wb_import.py import <file.json>     # 导入并验证凭证有效性
  python3 wb_import.py list                   # 列出已导入账号(不显示 token)
  python3 wb_import.py status [uid|all]       # 查询签到状态
  python3 wb_import.py checkin [uid|all]      # 直连领取每日积分
  python3 wb_import.py remove <uid|昵称>       # 移除已导入账号

仅使用 Python 标准库。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
STORE_FILE = HOME / ".workbuddy" / "token-accounts.json"
DEFAULT_DOMAIN = "www.workbuddy.cn"
API_CHECKIN = "/v2/billing/meter/daily-checkin"
API_STATUS = "/v2/billing/meter/checkin-activity-status"
HTTP_TIMEOUT = 20


# --------------------------------------------------------------------------
# 存储读写
# --------------------------------------------------------------------------
def load_store() -> dict:
    try:
        data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        accs = data.get("accounts") if isinstance(data, dict) else None
        if isinstance(accs, list):
            return {"version": 1, "accounts": [a for a in accs if isinstance(a, dict)]}
    except Exception:
        pass
    return {"version": 1, "accounts": []}


def save_store(store: dict) -> None:
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE_FILE)


# --------------------------------------------------------------------------
# 凭证解析
# --------------------------------------------------------------------------
def jwt_decode(token: str) -> dict:
    """解码 JWT payload(不验签,仅读取 exp/sub 等元信息)。"""
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def normalize_entry(raw: dict) -> dict | None:
    """把一条外部凭证规整为存储条目;缺 token 或无法识别 uid 时返回 None。"""
    if not isinstance(raw, dict):
        return None
    auth_raw = raw.get("auth_raw") if isinstance(raw.get("auth_raw"), dict) else {}
    nested = auth_raw.get("auth") if isinstance(auth_raw.get("auth"), dict) else {}
    device = raw.get("device") if isinstance(raw.get("device"), dict) else {}

    access = _first(raw, "access_token", "accessToken") or _first(nested, "accessToken")
    refresh = _first(raw, "refresh_token", "refreshToken") or _first(nested, "refreshToken")
    if not access or not refresh:
        return None

    try:
        at_claims = jwt_decode(access)
        rt_claims = jwt_decode(refresh)
    except Exception:
        return None

    uid = _first(raw, "uid") or at_claims.get("sub") or _first(nested, "uid")
    if not uid:
        return None
    preferred = str(at_claims.get("preferred_username") or "")
    phone = preferred if re.fullmatch(r"\d{11}", preferred) else ""
    domain = str(_first(raw, "domain", "authDomain") or nested.get("domain") or DEFAULT_DOMAIN)
    if not re.fullmatch(r"[\w.-]+", domain):  # 防注入到 URL
        domain = DEFAULT_DOMAIN

    return {
        "uid": str(uid),
        "nickname": str(_first(raw, "nickname") or auth_raw.get("nickname") or preferred or uid[:8]),
        "phone": phone,
        "domain": domain,
        "issuer": str(at_claims.get("iss") or f"https://{domain}/auth/realms/copilotot"),
        "client_id": str(at_claims.get("azp") or "console"),
        "access_token": str(access),
        "refresh_token": str(refresh),
        "access_exp": int(at_claims.get("exp") or 0),
        "refresh_exp": int(rt_claims.get("exp") or 0),
        "device_token": str(device.get("deviceToken") or raw.get("device_token") or ""),
        "enabled": bool(raw.get("enabled", True)),
        "addedAt": int(time.time()),
        "lastCheckin": None,
        "lastError": "",
    }


# --------------------------------------------------------------------------
# 官方 API(标准库直连)
# --------------------------------------------------------------------------
def api_post(account: dict, path: str, body: dict | None = None):
    """返回 (http_status, payload)。非 2xx 抛 URLError 由调用方处理。"""
    req = urllib.request.Request(
        f"https://{account['domain']}{path}",
        method="POST",
        data=json.dumps(body or {}).encode(),
        headers={
            "Authorization": f"Bearer {account['access_token']}",
            "Content-Type": "application/json",
            "X-Client-Platform": "web",
            "X-User-Id": account["uid"],
        })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "{}")
        except Exception:
            payload = {}
        return e.code, payload


def refresh_tokens(account: dict) -> tuple[bool, str]:
    """经 Keycloak 刷新端点续期;成功则原地更新 account。刷新会轮换 refresh_token。"""
    url = account["issuer"].rstrip("/") + "/protocol/openid-connect/token"
    form = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": account.get("client_id") or "console",
        "refresh_token": account["refresh_token"],
    }).encode()
    req = urllib.request.Request(url, method="POST", data=form,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            tok = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return False, f"刷新失败 HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, f"刷新异常: {e}"
    if not tok.get("access_token"):
        return False, "刷新响应缺少 access_token"
    try:
        at = jwt_decode(tok["access_token"])
        account["access_token"] = tok["access_token"]
        account["access_exp"] = int(at.get("exp") or 0)
        if tok.get("refresh_token"):
            account["refresh_token"] = tok["refresh_token"]
            account["refresh_exp"] = int(jwt_decode(tok["refresh_token"]).get("exp") or 0)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"刷新响应异常: {e}"


def _call_with_refresh(account: dict, path: str, body: dict | None = None):
    """调 API;401 时自动刷新一次并重试。返回 (ok, status, payload, refreshed)。"""
    status, payload = api_post(account, path, body)
    refreshed = False
    if status == 401:
        ok, err = refresh_tokens(account)
        if ok:
            refreshed = True
            status, payload = api_post(account, path, body)
        else:
            account["lastError"] = err
    return status, payload, refreshed


# --------------------------------------------------------------------------
# 业务操作
# --------------------------------------------------------------------------
def import_data(text: str) -> dict:
    """解析凭证 JSON 文本,验证后并入账号池(uid 去重,重复导入即更新凭证)。"""
    try:
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "output": f"JSON 解析失败: {e}", "imported": [], "skipped": []}
    entries = data if isinstance(data, list) else [data]

    store = load_store()
    by_uid = {a["uid"]: a for a in store["accounts"]}
    imported, skipped = [], []

    for raw in entries:
        entry = normalize_entry(raw if isinstance(raw, dict) else {})
        if entry is None:
            skipped.append({"name": _first(raw, "nickname", "uid") or "?", "reason": "缺少 access/refresh token"})
            continue
        # 验证凭证:调状态接口确认当前有效(顺带 401 自愈刷新)
        status, payload, refreshed = _call_with_refresh(entry, API_STATUS)
        if status != 200 or payload.get("code") not in (0, None):
            reason = "token 无效或已过期" + (f"(HTTP {status})" if status != 200 else "")
            skipped.append({"name": entry["nickname"], "reason": reason})
            continue
        if entry["uid"] in by_uid:
            old = by_uid[entry["uid"]]
            entry["addedAt"] = old.get("addedAt") or entry["addedAt"]
            entry["lastCheckin"] = old.get("lastCheckin")
            entry["lastError"] = ""
        by_uid[entry["uid"]] = entry
        imported.append({"uid": entry["uid"], "nickname": entry["nickname"],
                         "phone": entry["phone"], "refreshed": refreshed,
                         "refreshDays": round((entry["refresh_exp"] - time.time()) / 86400, 1)})

    if imported:
        store["accounts"] = sorted(by_uid.values(), key=lambda a: a.get("addedAt") or 0)
        save_store(store)
    out = f"导入完成: 成功 {len(imported)} 个" + (f",跳过 {len(skipped)} 个" if skipped else "")
    return {"ok": bool(imported), "output": out, "imported": imported, "skipped": skipped}


def list_accounts() -> list:
    """脱敏账号列表(不含 token),供展示层使用。"""
    now = time.time()
    out = []
    for a in load_store()["accounts"]:
        out.append({
            "uid": a["uid"],
            "nickname": a.get("nickname") or a["uid"][:8],
            "phone": a.get("phone", ""),
            "enabled": a.get("enabled", True),
            "accessLeftDays": round(max(0, a.get("access_exp", 0) - now) / 86400, 1),
            "refreshLeftDays": round(max(0, a.get("refresh_exp", 0) - now) / 86400, 1),
            "lastCheckin": a.get("lastCheckin"),
            "lastError": a.get("lastError", ""),
            "mtime": a.get("addedAt", 0),
        })
    return out


def _select(store: dict, key: str | None) -> list:
    accs = [a for a in store["accounts"] if a.get("enabled", True)]
    if not key or key == "all":
        return accs
    return [a for a in accs if a["uid"] == key or a.get("nickname") == key]


def checkin_accounts(key: str | None = None) -> list:
    """对选中账号直连领取每日积分;成功后把新 token 落盘(刷新可能轮换)。"""
    store = load_store()
    results = []
    touched = False
    for a in _select(store, key):
        status, payload, refreshed = _call_with_refresh(a, API_CHECKIN)
        touched |= refreshed
        if refreshed:  # 刷新成功,先落盘保住新 token
            save_store(store)
        code = payload.get("code")
        if status == 200 and code == 0:
            d = payload.get("data") or {}
            a["lastCheckin"] = int(time.time())
            a["lastError"] = ""
            save_store(store)
            results.append({"uid": a["uid"], "nickname": a["nickname"], "ok": True,
                            "msg": f"签到成功 +{d.get('credit')} 积分(连续 {d.get('streak_days')} 天)"})
        elif status == 200 and code == 10001:
            a["lastCheckin"] = int(time.time())
            a["lastError"] = ""
            save_store(store)
            results.append({"uid": a["uid"], "nickname": a["nickname"], "ok": True,
                            "msg": "今日已签过,无需重复领取"})
        else:
            a["lastError"] = f"HTTP {status} code={code} {payload.get('msg', '')}"[:120]
            save_store(store)
            results.append({"uid": a["uid"], "nickname": a["nickname"], "ok": False,
                            "msg": a["lastError"]})
    if touched:
        save_store(store)
    return results


def status_accounts(key: str | None = None) -> list:
    store = load_store()
    out = []
    for a in _select(store, key):
        status, payload, _ = _call_with_refresh(a, API_STATUS)
        d = payload.get("data") or {}
        out.append({"uid": a["uid"], "nickname": a["nickname"], "ok": status == 200,
                    "active": d.get("active"), "today_checked_in": d.get("today_checked_in"),
                    "streak_days": d.get("streak_days"), "error": a.get("lastError", "") if status != 200 else ""})
    return out


def remove_account(key: str) -> dict:
    store = load_store()
    rest = [a for a in store["accounts"]
            if not (a["uid"] == key or a.get("nickname") == key)]
    if len(rest) == len(store["accounts"]):
        return {"ok": False, "output": f"无此账号: {key}"}
    store["accounts"] = rest
    save_store(store)
    return {"ok": True, "output": f"已移除导入账号「{key}」"}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 登录凭证导入器(token 直连账号池)")
    ap.add_argument("command", choices=["import", "list", "status", "checkin", "remove"])
    ap.add_argument("arg", nargs="?", help="import=文件路径 | status/checkin=uid|all | remove=uid|昵称")
    args = ap.parse_args()

    if args.command == "import":
        if not args.arg:
            _emit({"ok": False, "output": "用法: wb_import.py import <file.json>"})
            return
        try:
            text = Path(args.arg).read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            _emit({"ok": False, "output": f"读取文件失败: {e}"})
            return
        _emit(import_data(text))
    elif args.command == "list":
        _emit({"ok": True, "accounts": list_accounts()})
    elif args.command == "status":
        _emit({"ok": True, "accounts": status_accounts(args.arg or "all")})
    elif args.command == "checkin":
        res = checkin_accounts(args.arg or "all")
        _emit({"ok": bool(res) and all(r["ok"] for r in res),
               "output": f"直连签到: {sum(1 for r in res if r['ok'])}/{len(res)} 成功",
               "results": res})
    elif args.command == "remove":
        if not args.arg:
            _emit({"ok": False, "output": "用法: wb_import.py remove <uid|昵称>"})
            return
        _emit(remove_account(args.arg))


if __name__ == "__main__":
    main()
