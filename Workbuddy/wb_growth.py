#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 成长中心任务自动化 —— 核心模块(供控制台(app)调用,亦可 CLI 使用)

两条执行通道(均为实测验证过的真实路径):
  1. wbipc 轻量通道:复用 wb_checkin.py 的 WbipcClient,让 WorkBuddy 应用进程
     用当前登录会话调 www.workbuddy.cn 成长中心官方 API(任务列表/接受/领奖/
     连续登录兑换/抽奖)。秒级完成,不需要浏览器。
  2. 桌面端 CDP 通道:WorkBuddy 以 --remote-debugging-port=9223 启动后,
     Playwright 连接真实 UI,驱动「聊天/召唤专家/体验灵感」等真实产品使用,
     服务端按真实行为计数(2026-10-16 全流程实测通过)。

用法:
  python3 wb_growth.py status     # 任务概览 JSON
  python3 wb_growth.py run chat_5 expert_5   # 执行指定任务(CLI 前台)
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from wb_checkin import WbipcClient, _wait_app  # noqa: E402

CDP_PORT = 9223
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

# --------------------------------------------------------------------------
# 任务注册表:type = api(wbipc 秒级) | cdp(驱动桌面端 UI) | manual(需人工)
# est 为预估自动化耗时(展示用);reward 为任务奖励(展示用)
# --------------------------------------------------------------------------
TASK_META = {
    "chat_5":                {"name": "和 AI 聊天 5 次",       "type": "cdp",    "est": "约 3 分钟"},
    "expert_5":              {"name": "召唤 5 次专家",         "type": "cdp",    "est": "约 8 分钟"},
    "Expert_team_use_3":     {"name": "召唤 3 次专家团",       "type": "cdp",    "est": "约 4 分钟"},
    "playbook_prompt":       {"name": "探索优秀灵感",          "type": "cdp",    "est": "约 4 分钟"},
    "Library_read":          {"name": "体验「资料库」",        "type": "cdp",    "est": "约 1 分钟"},
    "Buddy_App":             {"name": "体验「发现应用」",      "type": "cdp",    "est": "约 3 分钟"},
    "skill_1":               {"name": "尝鲜热门技能",          "type": "auto",   "est": "随专家任务自动完成"},
    "create_canvas":         {"name": "体验「设计创意模式」",   "type": "cdp",    "est": "约 5 分钟(实验)"},
    "template_5":            {"name": "使用 5 个模板",         "type": "cdp",    "est": "约 10 分钟(实验)"},
    "Hp_Appearance":         {"name": "体验「和平精英」主题",   "type": "cdp",    "est": "约 2 分钟(实验)"},
    "Expert_lighthouse":     {"name": "体验「腾讯轻量云」专家", "type": "manual", "est": "需连接器授权,建议手动"},
    "automation_1":          {"name": "设置自动化任务",         "type": "manual", "est": "弹窗一键创建,建议手动"},
    "wb_wechat_oa_subscribe_task": {"name": "关注官方公众号",   "type": "manual", "est": "需微信扫码,无法自动化"},
    "RichMeow_Chat":         {"name": "桌面端对话 1 次",       "type": "auto",   "est": "随聊天任务自动完成"},
    "first_buddy":           {"name": "领取一只 Buddy",        "type": "auto",   "est": "自动任务"},
}


# --------------------------------------------------------------------------
# wbipc 成长 API 客户端
# --------------------------------------------------------------------------
class GrowthApi:
    """经 wbipc(wb.request/http.fetch)调用成长中心官方 API。"""

    def __init__(self):
        _wait_app()
        self._c = WbipcClient()
        self._c.connect()

    def close(self):
        try:
            self._c.close()
        except Exception:
            pass

    def fetch(self, method: str, path: str, body: dict | None = None):
        r = self._c.rpc("broker/GetPipe", {"pipe": "wb.request"})
        channel = r["result"]["channel"]
        params = {"method": method, "path": path,
                  "headers": {"content-type": "application/json"}}
        if body is not None and method != "GET":
            params["body_b64"] = base64.b64encode(json.dumps(body).encode()).decode()
        r2 = self._c.rpc(f"{channel}/http.fetch", params)
        if "result" not in r2:
            raise RuntimeError(f"http.fetch 失败: {r2.get('error')}")
        res = r2["result"]
        payload = json.loads(base64.b64decode(res["body_b64"])) if res.get("body_b64") else {}
        return res.get("status"), payload

    # ---- 具体接口 ----
    def tasks(self) -> list:
        _, p = self.fetch("GET", "/v2/activity/growth/tasks")
        return (p.get("data") or {}).get("tasks") or []

    def profile(self) -> dict:
        _, p = self.fetch("GET", "/v2/activity/growth/profile")
        return p.get("data") or {}

    def energy(self) -> int:
        _, p = self.fetch("GET", "/activity/growth/energy")
        return (p.get("data") or {}).get("balance") or 0

    def accept(self, codes: list) -> bool:
        if not codes:
            return True
        _, p = self.fetch("POST", "/activity/growth/tasks/accept", {"task_codes": codes})
        return p.get("code") == 0

    def claim(self, code: str) -> dict:
        _, p = self.fetch("POST", f"/activity/growth/tasks/{code}/claim")
        return p

    def streak(self) -> dict:
        _, p = self.fetch("GET", "/activity/growth/streak")
        return p.get("data") or {}

    def redeem(self, tier: str) -> dict:
        import uuid
        return self.fetch("POST", "/activity/growth/redeem",
                          {"tier": tier, "client_token": f"u-{uuid.uuid4()}"})[1]

    def lottery_chances(self) -> int:
        _, p = self.fetch("GET", "/activity/growth/lottery/chances")
        return (p.get("data") or {}).get("chances") or 0

    def lottery_draw(self) -> dict:
        import uuid
        return self.fetch("POST", "/activity/growth/lottery/draw",
                          {"client_token": f"u-{uuid.uuid4()}"})[1]


# --------------------------------------------------------------------------
# 概览(按钮数字 + 弹窗列表数据)
# --------------------------------------------------------------------------
def get_status() -> dict:
    api = GrowthApi()
    try:
        tasks = api.tasks()
        profile = api.profile()
        energy = api.energy()
    finally:
        api.close()
    out = []
    done = 0
    for t in tasks:
        code = t.get("task_code", "")
        meta = TASK_META.get(code, {})
        st = t.get("accept_status", "?")
        prog = t.get("progress") or {}
        if st == "claimed":
            done += 1
        out.append({
            "code": code,
            "title": t.get("title") or meta.get("name") or code,
            "status": st,
            "current": prog.get("current") or 0,
            "target": prog.get("target") or 0,
            "credit": t.get("reward_credit", 0),
            "energy": t.get("reward_energy", 0),
            "type": meta.get("type", "manual"),
            "est": meta.get("est", "—"),
            "automatable": meta.get("type") in ("api", "cdp", "auto"),
        })
    return {
        "ok": True,
        "completed": done,
        "total": len(out),
        "level": profile.get("level"),
        "levelName": profile.get("level_name"),
        "energy": energy,
        "tasks": out,
    }


# --------------------------------------------------------------------------
# 桌面端 CDP 动作(移植自 Workbuddy/desktop.py 已验证流程)
# --------------------------------------------------------------------------
def _pw():
    from playwright.sync_api import sync_playwright
    return sync_playwright()


def _main_page(browser):
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "index.html" in p.url:
                return p
    return None


def _click_visible_text(page, text: str) -> bool:
    return page.evaluate(
        """(text) => {
            const els = [...document.querySelectorAll('button, div, span')]
                .filter(el => (el.innerText || '').trim() === text);
            const visible = els.find(el => {
                const r = el.getBoundingClientRect();
                return r.width > 10 && r.height > 10;
            });
            if (visible) { visible.click(); return true; }
            return false;
        }""", text)


def _send_message(page, text: str, wait: bool = True) -> bool:
    try:
        editor = page.locator(".cr-input-editor-host").first
        editor.click(timeout=10000)
        page.wait_for_timeout(400)
        page.keyboard.type(text, delay=25)
        page.wait_for_timeout(300)
        page.locator(".cr-input-toolbar__send button").first.click(timeout=5000)
    except Exception:
        return False
    if wait:
        _wait_response(page)
    return True


def _wait_response(page, timeout: int = 150) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        done = page.evaluate(
            """() => [...document.querySelectorAll('*')].some(el =>
                el.children.length === 0 && el.innerText && el.innerText.includes('共消耗'))""")
        if done:
            return True
    return False


def _open_expert_panel(page):
    try:
        page.get_by_text("新建任务", exact=True).first.click(timeout=5000)
        page.wait_for_timeout(1200)
    except Exception:
        pass
    page.get_by_text("专家·技能·连接器", exact=True).first.click(timeout=8000)
    page.wait_for_timeout(2000)


def _handle_disclaimer(page):
    page.wait_for_timeout(1500)
    page.evaluate(
        """() => {
            const b = [...document.querySelectorAll('button')].find(x =>
                (x.innerText || '').includes('悉知并继续使用'));
            if (b) b.click();
        }""")


def _summon_expert(page, name: str) -> bool:
    """召唤一位专家并发一条消息(实测计数路径)。"""
    _open_expert_panel(page)
    if not _click_visible_text(page, name):
        return False
    page.wait_for_timeout(2500)
    _handle_disclaimer(page)
    _click_visible_text(page, "召唤")
    page.wait_for_timeout(3000)
    _handle_disclaimer(page)
    page.wait_for_timeout(1000)
    if not _send_message(page, "请直接回复两个字:收到。不要提问,不要问卷。"):
        return False
    _wait_response(page)
    return True


# 各任务的 CDP 动作(code -> callable(page) -> bool)
def _act_chat(page, need: int = 5) -> bool:
    prompts = ["你好,请只回复:收到", "用一句话介绍你自己", "1+1等于几?只回答数字",
               "今天天气不错,说句吉利话", "请回复:好的,再见", "讲一个五十字以内的小笑话"]
    ok = 0
    for i in range(need):
        if _send_message(page, prompts[i % len(prompts)]):
            ok += 1
        page.wait_for_timeout(2000)
    return ok >= need


def _act_expert(page, names, need: int = 5) -> bool:
    done = 0
    for name in names:
        if done >= need:
            break
        if _summon_expert(page, name):
            done += 1
        page.wait_for_timeout(2000)
    return done >= need


def _act_buddy_app(page) -> bool:
    try:
        page.get_by_text("新建任务", exact=True).first.click(timeout=5000)
        page.wait_for_timeout(1500)
    except Exception:
        pass
    if not _click_visible_text(page, "美图设计室"):
        page.wait_for_timeout(1500)
        if not _click_visible_text(page, "美图设计室"):
            return False
    page.wait_for_timeout(2500)
    page.evaluate(
        """() => {
            const b = [...document.querySelectorAll('button')].find(x =>
                (x.innerText || '').includes('确认授权'));
            if (b) b.click();
        }""")
    page.wait_for_timeout(4000)
    page.evaluate(
        """() => {
            const els = [...document.querySelectorAll('button, span, a, div')].filter(el =>
                (el.innerText || '').trim() === '跳过');
            const v = els.find(el => el.getBoundingClientRect().width > 5);
            if (v) v.click();
        }""")
    page.wait_for_timeout(3000)
    return _send_message(page, "请帮我设计一张简约风格的活动海报")


def _act_library(page) -> bool:
    subprocess.run(["open", "workbuddy://library"])
    page.wait_for_timeout(5000)
    clicked = page.evaluate(
        """() => {
            const els = [...document.querySelectorAll('div, span, button')].filter(el =>
                (el.innerText || '').trim().startsWith('了解资料库'));
            const t = els.sort((a, b) =>
                a.getBoundingClientRect().width - b.getBoundingClientRect().width)[0];
            if (t) { t.click(); return true; } return false;
        }""")
    if not clicked:
        return False
    page.wait_for_timeout(3500)
    page.evaluate(
        """() => {
            const els = [...document.querySelectorAll('div, span')].filter(el => {
                const t = (el.innerText || '').trim();
                return t.startsWith('WorkBuddy资料库介绍') || t.startsWith('Workbuddy资料库介绍');
            });
            const t = els.sort((a, b) =>
                a.getBoundingClientRect().width - b.getBoundingClientRect().width)[0];
            if (t) t.click();
        }""")
    page.wait_for_timeout(3000)
    for _ in range(5):
        page.mouse.wheel(0, 600)
        page.wait_for_timeout(900)
    return True


def _act_playbook(page) -> bool:
    subprocess.run(["open", "workbuddy://playbook"])
    page.wait_for_timeout(5000)
    ok = page.evaluate(
        """() => {
            const els = [...document.querySelectorAll('div, span, button')].filter(el =>
                (el.innerText || '').trim().startsWith('大学四年') ||
                (el.innerText || '').trim().startsWith('泡泡玛特'));
            const t = els.filter(e => e.getBoundingClientRect().width > 100 && e.getBoundingClientRect().width < 400)
                .sort((a, b) => a.getBoundingClientRect().width - b.getBoundingClientRect().width)[0];
            if (!t) return false;
            const r = t.getBoundingClientRect();
            t.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, clientX: r.x + r.width/2, clientY: r.y + r.height/2}));
            t.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, clientX: r.x + r.width/2, clientY: r.y + r.height/2}));
            t.click();
            return true;
        }""")
    if not ok:
        return False
    page.wait_for_timeout(3000)
    made = page.evaluate(
        """() => {
            const b = [...document.querySelectorAll('button')].find(x =>
                (x.innerText || '').trim() === '做同款');
            if (b) { b.click(); return true; } return false;
        }""")
    if not made:
        return False
    page.wait_for_timeout(4000)
    page.evaluate("document.body.click()")
    page.wait_for_timeout(1000)
    try:
        page.locator(".cr-input-toolbar__send button").first.click(timeout=8000)
        page.wait_for_timeout(8000)
        return True
    except Exception:
        return False


CDP_ACTIONS = {
    "chat_5": lambda page: _act_chat(page, 5),
    "Expert_team_use_3": lambda page: _act_expert(
        page, ["内容创作专家团", "交易分析团队", "财税合规专家团"], 3),
    "expert_5": lambda page: _act_expert(
        page, ["论文写作导师", "校园活动策划与执行顾问", "内容创作专家",
               "小红书运营专家", "股票研究专家", "法律检索专家", "创业伙伴"], 5),
    "Buddy_App": _act_buddy_app,
    "Library_read": _act_library,
    "playbook_prompt": _act_playbook,
}

EXPERT_NAMES_INDIVIDUAL = ["论文写作导师", "校园活动策划与执行顾问", "内容创作专家",
                           "小红书运营专家", "股票研究专家", "法律检索专家", "创业伙伴"]


def cdp_available() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------
def run_tasks(codes: list, progress=None) -> dict:
    """执行勾选的任务。progress(step_msg: str) 用于上报进度。"""
    def report(msg):
        if progress:
            progress(msg)

    api = GrowthApi()
    results = []
    try:
        # 0) 先接受所有未接受任务(接受后使用才计数)
        tasks = api.tasks()
        pending = [t["task_code"] for t in tasks if t.get("accept_status") == "not_accepted"]
        if pending:
            api.accept(pending)
            report(f"已接受 {len(pending)} 个任务")

        page = None
        pw_ctx = None
        browser = None
        try:
            for code in codes:
                meta = TASK_META.get(code, {})
                task = next((t for t in tasks if t.get("task_code") == code), None)
                if task and task.get("accept_status") == "claimed":
                    results.append({"code": code, "ok": True, "msg": "已完成并领取过,跳过"})
                    continue
                if task and task.get("accept_status") == "completed":
                    r = api.claim(code)
                    results.append({"code": code, "ok": r.get("code") == 0,
                                    "msg": "任务已完成,领取奖励" + ("成功" if r.get("code") == 0 else f"失败:{r.get('msg')}")})
                    continue
                if meta.get("type") == "manual":
                    results.append({"code": code, "ok": False,
                                    "msg": f"该任务需手动完成({meta.get('est')})"})
                    continue
                if code in CDP_ACTIONS:
                    # CDP 任务:按需建立连接
                    if page is None:
                        if not cdp_available():
                            results.append({"code": code, "ok": False,
                                            "msg": "桌面端调试端口未开启(先运行 Workbuddy/desktop.py ensure)"})
                            continue
                        report(f"连接桌面端 CDP …")
                        pw_ctx = _pw().start()
                        browser = pw_ctx.chromium.connect_over_cdp(CDP_URL)
                        page = _main_page(browser)
                        if page is None:
                            results.append({"code": code, "ok": False, "msg": "未找到主窗口"})
                            continue
                    report(f"执行「{meta.get('name', code)}」({meta.get('est')})…")
                    try:
                        ok = CDP_ACTIONS[code](page)
                    except Exception as e:  # noqa: BLE001
                        ok = False
                        report(f"  动作异常: {type(e).__name__}: {e}")
                    # 等待服务端计数并尝试领奖
                    page.wait_for_timeout(5000)
                    t2 = next((t for t in api.tasks() if t.get("task_code") == code), None)
                    st2 = (t2 or {}).get("accept_status")
                    if st2 == "completed":
                        r = api.claim(code)
                        got = "已领奖" if r.get("code") == 0 else f"领奖失败:{r.get('msg')}"
                        results.append({"code": code, "ok": True,
                                        "msg": f"动作{'成功' if ok else '部分完成'},任务完成,{got}"})
                    elif st2 == "claimed":
                        results.append({"code": code, "ok": True, "msg": "任务完成,奖励已领取"})
                    else:
                        prog = (t2 or {}).get("progress") or {}
                        results.append({"code": code, "ok": ok,
                                        "msg": f"动作{'已执行' if ok else '失败'},进度 {prog.get('current', 0)}/{prog.get('target', '?')}(服务端计数可能延迟)"})
                    continue
                # auto 型 / 直接领奖型
                report(f"处理「{meta.get('name', code)}」…")
                t2 = next((t for t in api.tasks() if t.get("task_code") == code), None)
                st2 = (t2 or {}).get("accept_status")
                if st2 == "completed":
                    r = api.claim(code)
                    results.append({"code": code, "ok": r.get("code") == 0,
                                    "msg": "领取奖励" + ("成功" if r.get("code") == 0 else f"失败:{r.get('msg')}")})
                elif st2 == "claimed":
                    results.append({"code": code, "ok": True, "msg": "已完成,跳过"})
                else:
                    results.append({"code": code, "ok": False, "msg": "该任务暂不能自动完成(需真实产品使用)"})
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if pw_ctx:
                try:
                    pw_ctx.stop()
                except Exception:
                    pass

        # 收尾:领取所有已完成任务奖励
        report("领取所有已完成任务奖励 …")
        claimed = 0
        for t in api.tasks():
            if t.get("accept_status") == "completed":
                r = api.claim(t["task_code"])
                if r.get("code") == 0:
                    claimed += 1
        if claimed:
            report(f"额外领取 {claimed} 个任务奖励")

        # 连续登录兑换
        try:
            streak = api.streak()
            rs = streak.get("redemption_status") or {}
            for tier in ("7d", "14d", "28d"):
                if rs.get(f"tier_{tier}_status") == "claimable":
                    r = api.redeem(tier)
                    if r.get("code") == 0:
                        report(f"兑换连续登录 {tier} 档奖励成功")
        except Exception:
            pass

        # 抽奖
        try:
            chances = api.lottery_chances()
            for _ in range(chances):
                api.lottery_draw()
                time.sleep(1)
            if chances:
                report(f"抽奖 {chances} 次")
        except Exception:
            pass

        ok_count = sum(1 for r in results if r["ok"])
        return {"ok": ok_count == len(results) if results else True,
                "output": f"任务执行完毕: {ok_count}/{len(results)} 成功",
                "results": results}
    finally:
        api.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "status":
        print(json.dumps(get_status(), ensure_ascii=False, indent=1))
    elif cmd == "run":
        codes = sys.argv[2:]
        if not codes:
            print("用法: wb_growth.py run <task_code> …(见 status 输出)")
            return
        r = run_tasks(codes, progress=lambda m: print(f"  {m}", flush=True))
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("用法: wb_growth.py status | run <task_code>…")


if __name__ == "__main__":
    main()
