"""签到编排：逐账号「切换 → 等待生效 → 领取」，支持钩子、直连账号与自动调度。"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time

from app.agents import all_agents
from app.core import config, locks, logs


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


def agent_checkin(adapter, only_account: str | None = None, trigger: str = "manual") -> dict:
    """对一个 agent 执行签到：逐个切换账号刷新登录态，最后按需切回原账号。"""
    cfg = config.load_config(list(all_agents()))
    pcfg = cfg["platforms"].get(adapter.id, {})
    results = []

    with locks.checkin_lock:
        state = adapter.state()
        accounts = state["accounts"]
        if not accounts:
            msg = "暂无账号快照，请先在应用内登录并用「添加账号」保存"
            logs.append_log(adapter.id, False, msg, trigger=trigger)
            return {"ok": False, "output": msg, "results": results}

        if only_account:
            targets = [a["name"] for a in accounts if a["name"] == only_account]
            if not targets:
                msg = f"无此账号快照: {only_account}"
                logs.append_log(adapter.id, False, msg, account=only_account, trigger=trigger)
                return {"ok": False, "output": msg, "results": results}
        else:
            targets = [a["name"] for a in accounts]

        original = next((a["name"] for a in accounts if a["active"]), None)
        disp = {a["name"]: a.get("display") or a["name"] for a in accounts}

        for name in targets:
            try:
                # API 直连账号（如导入的 token 池）：直接领取，不切换桌面应用
                direct = adapter.direct_checkin(name)
                if direct is not None:
                    r0 = direct
                    logs.append_log(adapter.id, bool(r0.get("ok")),
                                    f"[签到] {disp[name]}: {r0.get('msg', '')}",
                                    account=disp[name], trigger=trigger)
                    results.append({"account": disp[name], "ok": bool(r0.get("ok")),
                                    "msg": r0.get("msg", "")})
                    continue
                cur = adapter.state()
                active = next((a["name"] for a in cur["accounts"] if a["active"]), None)
                switched = False
                t_switch = int(time.time())
                if active != name or adapter.always_relaunch:
                    r = adapter.switch(name)
                    if not r.get("ok"):
                        out = r.get("output", "切换失败")
                        logs.append_log(adapter.id, False, f"[签到] {disp[name]}: 切换失败 — {out}",
                                        account=disp[name], trigger=trigger)
                        results.append({"account": disp[name], "ok": False, "msg": out})
                        continue
                    switched = True
                    time.sleep(adapter.settle_seconds)

                hook = (pcfg.get("checkin_command") or "").strip()
                if hook:
                    # 自定义钩子优先级最高
                    ok, out = _run_hook(hook, adapter.id, name)
                    msg = f"[签到] {disp[name]}: 钩子{'成功' if ok else '失败'} — {out[:200]}"
                    logs.append_log(adapter.id, ok, msg, account=disp[name], trigger=trigger)
                    results.append({"account": disp[name], "ok": ok, "msg": out[:200]})
                elif adapter.has_claim:
                    # 平台原生领取：WorkBuddy 经 wbipc 代领；Trae 由扩展领取并确认日志
                    ok, out = adapter.claim(disp[name], t_switch)
                    if ok is None:  # 异步领取中（如应用内扩展），尚未出结果
                        msg = f"[签到] {disp[name]}: 已切换并重启，扩展领取中（异步，稍后见日志）"
                        logs.append_log(adapter.id, True, msg, account=disp[name], trigger=trigger)
                        results.append({"account": disp[name], "ok": True, "msg": "扩展领取中"})
                    else:
                        logs.append_log(adapter.id, bool(ok), f"[签到] {disp[name]}: {out}",
                                        account=disp[name], trigger=trigger)
                        results.append({"account": disp[name], "ok": bool(ok), "msg": out})
                else:
                    note = adapter.sign_note if switched else "当前已是该账号，登录态在线有效"
                    logs.append_log(adapter.id, True, f"[签到] {disp[name]}: {note}",
                                    account=disp[name], trigger=trigger)
                    results.append({"account": disp[name], "ok": True, "msg": note})
            except Exception as e:  # noqa: BLE001
                logs.append_log(adapter.id, False,
                                f"[签到] {disp.get(name, name)}: 异常 — {type(e).__name__}: {e}",
                                account=disp.get(name, name), trigger=trigger)
                results.append({"account": disp.get(name, name), "ok": False, "msg": str(e)})

        # 切回原来的账号，避免影响用户当前使用
        if cfg.get("restore_after_checkin") and original and len(targets) > 1:
            try:
                cur = adapter.state()
                active = next((a["name"] for a in cur["accounts"] if a["active"]), None)
                if active != original:
                    adapter.switch(original)
                    logs.append_log(adapter.id, True,
                                    f"[签到] 已切回签到前账号「{disp[original]}」",
                                    account=disp[original], trigger=trigger)
            except Exception as e:  # noqa: BLE001
                logs.append_log(adapter.id, False, f"[签到] 切回原账号失败: {e}", trigger=trigger)

    ok_all = bool(results) and all(r["ok"] for r in results)
    done = sum(1 for r in results if r["ok"])
    return {"ok": ok_all,
            "output": f"{adapter.label}: {done}/{len(results)} 个账号签到成功",
            "results": results}


def checkin_all(trigger: str = "manual") -> dict:
    cfg = config.load_config(list(all_agents()))
    summary = []
    for aid, adapter in all_agents().items():
        if not cfg["platforms"].get(aid, {}).get("enabled", True):
            continue
        ok_os, reason = adapter.available()
        if not ok_os:
            summary.append({"platform": aid, "label": adapter.label,
                            "ok": False, "output": f"{adapter.label}: {reason}"})
            continue
        res = agent_checkin(adapter, trigger=trigger)
        summary.append({"platform": aid, "label": adapter.label,
                        "ok": res["ok"], "output": res["output"]})
    return {"ok": all(s["ok"] for s in summary) if summary else False, "summary": summary}


async def scheduler_loop():
    """自动签到调度：到点（或补签）自动对启用平台执行全账号签到。"""
    while True:
        try:
            cfg = config.load_config(list(all_agents()))
            if cfg.get("auto_enabled"):
                today = time.strftime("%Y-%m-%d")
                hh, mm = (cfg.get("auto_time") or "05:20").split(":")
                target = int(hh) * 60 + int(mm)
                now = time.localtime()
                now_min = now.tm_hour * 60 + now.tm_min
                due = now_min >= target if cfg.get("auto_resign") else target <= now_min <= target + 1
                if due and cfg.get("last_auto_date") != today:
                    cfg["last_auto_date"] = today
                    config.save_config(cfg)
                    logs.append_log("system", True,
                                    f"[自动签到] 到点 {cfg.get('auto_time')}，开始全平台签到",
                                    trigger="auto")
                    await asyncio.to_thread(checkin_all, "auto")
        except Exception as e:  # noqa: BLE001
            logs.append_log("system", False,
                            f"[自动签到] 调度异常: {type(e).__name__}: {e}", trigger="auto")
        await asyncio.sleep(30)
