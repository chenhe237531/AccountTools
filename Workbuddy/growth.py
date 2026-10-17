#!/usr/bin/env python3
"""WorkBuddy 成长中心任务自动化工具

自动完成 https://www.workbuddy.cn/profile/growth-center 的成长任务并获得积分/能量。

原理:
  成长中心的任务列表/接受/领奖/兑换/抽奖均为 www.workbuddy.cn 站内 HTTP API;
  任务进度由服务端统计真实产品使用得出。本工具用 Playwright 驱动一个**专用
  Chrome 持久会话**(登录态保存在本地浏览器 profile,凭据不导出):
    - 站内 API 在页面上下文内以官方前端相同方式 fetch(自动带 Cookie);
    - 聊天类任务通过产品会话 API + ACP 协议发送真实消息(与桌面端同源,
      服务端按真实使用计数);
    - 桌面端专属任务(如换主题)给出指引,完成后再运行即可自动领奖。

用法:
  python3 growth.py login                # 首次使用:打开浏览器窗口扫码/手机号登录
  python3 growth.py login --cookies F    # 或:导入浏览器插件导出的 Cookie JSON 文件
  python3 growth.py status               # 查看任务/进度/积分与能量
  python3 growth.py run                  # 一键:接受任务 → 自动聊天 → 领奖 → 兑换/抽奖
  python3 growth.py chat "你好"          # 手动发一条真实聊天(调试/凑对话次数)
  python3 growth.py accept               # 只接受全部任务
  python3 growth.py claim                # 只领取已完成奖励
  python3 growth.py redeem               # 兑换连续登录奖励(7/14/28 天档)
  python3 growth.py lottery              # 抽奖(有机会次数时)
  python3 growth.py gacha                # 能量开 Buddy 盲盒(能量>=10)

会话目录: ~/.workbuddy/growth-profile   (删除该目录即登出)
依赖: pip install playwright ;系统需装有 Google Chrome
"""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

HOME = Path.home()
PROFILE_DIR = HOME / ".workbuddy/growth-profile"
SESSION_FILE = HOME / ".workbuddy/growth-session.json"   # Cookie 引导文件(自愈用)
ORIGIN = "https://www.workbuddy.cn"
GROWTH_PAGE = f"{ORIGIN}/profile/growth-center"

# ---- 成长中心站内 API ----
API_TASKS = "/v2/activity/growth/tasks"
API_PROFILE = "/v2/activity/growth/profile"
API_ENERGY = "/activity/growth/energy"
API_ACCEPT = "/activity/growth/tasks/accept"
API_CLAIM = "/activity/growth/tasks/{code}/claim"
API_REDEEM = "/activity/growth/redeem"          # {tier, client_token}
API_REDEEM_SUMMARY = "/activity/growth/redeem/summary"
API_LOTTERY_SUMMARY = "/activity/growth/lottery/summary"
API_LOTTERY_DRAW = "/activity/growth/lottery/draw"  # {client_token}
API_LOTTERY_CHANCES = "/activity/growth/lottery/chances"
API_STREAK = "/activity/growth/streak"
API_BUDDY_INFO = "/activity/growth/buddy/info"

# ---- 产品会话 API(聊天任务) ----
API_CONV_CREATE = "/v2/as/conversations/"       # POST {conversationOrigin}
API_CONV_SESSION = "/v2/as/conversations/{cid}/session"  # POST 创建沙盒 / GET 取凭据
API_AGENTS = "/v2/user/cloudagent/agents"       # GET 云端专家(agents)列表

STATUS_TEXT = {
    "not_accepted": "未接受",
    "accepted": "已接受",
    "in_progress": "进行中",
    "completed": "可领奖",
    "claimed": "已领取",
}


def log(msg: str):
    print(msg, flush=True)


# ============================================================
# 浏览器会话
# ============================================================

def launch(pw, visible: bool = False):
    """启动持久化浏览器会话。

    重要:workbuddy.cn 的 WAF 能识别无头浏览器(TLS/HTTP2 指纹层面,UA 伪装无效),
    无头模式会被 401 并清除 session cookie。因此**始终使用有头模式**;
    非交互运行时把窗口移到屏幕外,用户无感知。
    """
    return pw.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        channel="chrome",
        headless=False,
        no_viewport=not visible,
        args=(
            ["--window-position=-32000,-32000", "--window-size=1280,900"]
            if not visible else ["--window-size=1280,900"]
        ) + ["--disable-blink-features=AutomationControlled"],
    )


def is_logged_in(page) -> bool:
    try:
        status = page.evaluate("""async () => {
            const r = await fetch('/v2/activity/growth/profile', {credentials:'include'});
            return r.status;
        }""")
        return status not in (401, 403)
    except Exception:  # noqa: BLE001
        return False


def open_growth_page(ctx, wait_login: bool = False):
    """打开工作页面。

    日常操作使用轻量同源页(robots.txt)——只作为 fetch 的执行上下文,
    无 JS、加载快、不产生多余流量。交互登录(wait_login)时加载真实页面。
    会话自愈:登录态失效时自动从 SESSION_FILE 重导入 Cookie 再验证。
    """
    page = ctx.new_page()
    if wait_login:
        page.goto(GROWTH_PAGE, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3000)
        if is_logged_in(page):
            return page
        log("[提示] 请在打开的浏览器窗口中登录(扫码/手机号均可),等待登录完成…")
        deadline = time.time() + 300
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            if is_logged_in(page):
                log("[提示] 检测到登录成功!")
                return page
        page.close()
        raise SystemExit("[错误] 5 分钟内未完成登录,请重试 python3 growth.py login")
    # 轻量同源页
    page.goto(f"{ORIGIN}/robots.txt", wait_until="commit", timeout=60_000)
    page.wait_for_timeout(1500)
    if is_logged_in(page):
        return page
    # 自愈:从引导文件重导入
    if SESSION_FILE.is_file():
        log("[会话] 登录态失效,尝试从引导文件恢复…")
        import_cookies(ctx, str(SESSION_FILE))
        page.reload(wait_until="commit")
        page.wait_for_timeout(1500)
        if is_logged_in(page):
            log("[会话] 恢复成功")
            return page
    page.close()
    raise SystemExit("[错误] 尚未登录或登录态失效。请先执行: python3 growth.py login")


def import_cookies(ctx, cookie_file: str) -> int:
    """导入浏览器插件(EditThisCookie/Cookie-Editor)导出的 Cookie JSON。"""
    data = json.loads(Path(cookie_file).read_text(encoding="utf-8"))
    cookies = []
    for c in data:
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain") or ".workbuddy.cn",
            "path": c.get("path") or "/",
        }
        if c.get("expirationDate"):
            cookie["expires"] = int(c["expirationDate"])
        if c.get("secure") is not None:
            cookie["secure"] = bool(c.get("secure"))
        if c.get("httpOnly") is not None:
            cookie["httpOnly"] = bool(c.get("httpOnly"))
        if str(cookie["domain"]).lstrip(".").endswith("workbuddy.cn"):
            cookies.append(cookie)
    for cookie in cookies:  # 逐条导入,避免批量静默失败
        ctx.add_cookies([cookie])
    return len(cookies)


# ============================================================
# 站内 API(页面上下文内执行,自动携带 Cookie)
# ============================================================

def api(page, method: str, path: str, body=None):
    result = page.evaluate("""
        async ([method, path, body]) => {
          const resp = await fetch(path, {
            method, credentials: 'include',
            headers: { 'Content-Type': 'application/json', 'X-Client-Platform': 'web' },
            body: body === null ? undefined : JSON.stringify(body),
          });
          let data = null;
          try { data = await resp.json(); } catch (e) {}
          return { httpStatus: resp.status, data };
        }
    """, [method, path, body])
    if result["httpStatus"] == 401:
        raise SystemExit("[错误] 登录态失效(401)。请重新执行: python3 growth.py login")
    return result["data"] or {}


def data_of(resp):
    return resp.get("data") or {}


def fetch_tasks(page):
    return data_of(api(page, "GET", API_TASKS)).get("tasks") or []


def fetch_energy(page):
    return data_of(api(page, "GET", API_ENERGY)).get("balance")


def fetch_profile(page):
    return data_of(api(page, "GET", API_PROFILE))


def show_status(page):
    profile = fetch_profile(page)
    energy = fetch_energy(page)
    tasks = fetch_tasks(page)
    log("")
    log(f"  等级:{profile.get('level_name') or profile.get('level')}   "
        f"任务完成:{profile.get('completed')}/{profile.get('total')}   "
        f"能量余额:{energy}")
    log("  " + "-" * 72)
    for t in tasks:
        code = t.get("task_code", "?")
        title = t.get("title", "?")
        credit = t.get("reward_credit", 0)
        enr = t.get("reward_energy", 0)
        st = t.get("accept_status", "?")
        prog = t.get("progress") or {}
        cur, tgt = prog.get("current"), prog.get("target")
        prog_txt = f" 进度 {cur}/{tgt}" if tgt else ""
        log(f"  [{STATUS_TEXT.get(st, st):<3}] {title}{prog_txt}  "
            f"(⚡{credit} ◇{enr})  code={code}")
    log("")


# ============================================================
# 任务:接受 / 领取
# ============================================================

def accept_all(page) -> int:
    tasks = fetch_tasks(page)
    pending = [t["task_code"] for t in tasks if t.get("accept_status") == "not_accepted"]
    if not pending:
        log("  没有待接受的任务")
        return 0
    resp = api(page, "POST", API_ACCEPT, {"task_codes": pending})
    ok = resp.get("code") == 0
    log(f"  接受 {len(pending)} 个任务: {'成功' if ok else '失败 ' + str(resp.get('msg'))}")
    return len(pending) if ok else 0


def claim_all(page) -> int:
    tasks = fetch_tasks(page)
    n = credit_total = energy_total = 0
    for t in tasks:
        if t.get("accept_status") == "completed":
            resp = api(page, "POST", API_CLAIM.format(code=t["task_code"]))
            if resp.get("code") == 0:
                d = data_of(resp)
                c = d.get("credit") or t.get("reward_credit") or 0
                e = d.get("energy") or t.get("reward_energy") or 0
                credit_total += c
                energy_total += e
                n += 1
                log(f"  ✓ 领取: {t.get('title')}  (+⚡{c} +◇{e})")
            else:
                log(f"  领取失败: {t.get('title')} ({resp.get('msg')})")
    if n:
        log(f"  合计获得: ⚡{credit_total} 积分, ◇{energy_total} 能量")
    else:
        log("  当前没有可领取的奖励")
    return n


# ============================================================
# 真实聊天(ACP 协议,与桌面端同源,服务端按真实使用计数)
# ============================================================

def create_sandbox_session(page, agent_id=None):
    """创建产品会话,返回 {cid, link, token, cwd}。

    会话创建响应内联包含 session 凭据(link/token);若未内联则回退到
    POST/GET /v2/as/conversations/{cid}/session 流程。
    """
    body = {"conversationOrigin": "agents"}
    if agent_id:
        body["agentId"] = agent_id
    conv = data_of(api(page, "POST", API_CONV_CREATE, body))
    cid = conv.get("id") or conv.get("conversationId") or conv.get("conversation_id")
    if not cid:
        raise RuntimeError(f"创建会话失败: {conv}")
    sess = conv.get("session") or {}
    if sess.get("link") and sess.get("token"):
        return {"cid": str(cid), "link": sess["link"], "token": sess["token"],
                "cwd": sess.get("cwd") or "/workspace"}
    # 回退:显式创建并轮询沙盒 session
    api(page, "POST", API_CONV_SESSION.format(cid=cid), {})
    for _ in range(20):
        time.sleep(3)
        s = data_of(api(page, "GET", API_CONV_SESSION.format(cid=cid)))
        if s.get("link") and s.get("token"):
            return {"cid": str(cid), "link": s["link"], "token": s["token"],
                    "cwd": s.get("cwd") or "/workspace"}
    raise RuntimeError("沙盒 session 60 秒内未就绪")


class AcpClient:
    """最小 ACP( Agent Client Protocol ) JSON-RPC 客户端。"""

    def __init__(self, link: str, token: str):
        self.link = link.rstrip("/")
        self.token = token
        self.base = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-codebuddy-request": "1",
            "Authorization": f"Bearer {token}",
        }
        self.next_id = 1
        r = requests.post(f"{self.link}/connect", headers=self.base, timeout=30)
        r.raise_for_status()
        creds = r.json()
        if not creds.get("connectionId"):
            raise RuntimeError(f"ACP connect 失败: {creds}")
        self.base["acp-connection-id"] = creds["connectionId"]
        if creds.get("sessionToken"):
            self.base["acp-session-token"] = creds["sessionToken"]

    def call(self, method: str, params: dict, timeout=180):
        rid = self.next_id
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        r = requests.post(self.link, headers=self.base, json=payload, timeout=timeout)
        r.raise_for_status()
        text = r.text
        # 响应可能是 JSON,也可能夹带 SSE 帧
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("id") == rid:
                    return obj
            except json.JSONDecodeError:
                continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text[:500]}

    def initialize(self):
        return self.call("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "workbuddy-acp-client", "version": "1.0.0"},
            "clientCapabilities": {"fs": {}},
        }, timeout=30)

    def new_session(self, cwd="/workspace"):
        resp = self.call("session/new", {"cwd": cwd, "mcpServers": []}, timeout=60)
        sid = (resp.get("result") or {}).get("sessionId")
        if not sid:
            raise RuntimeError(f"session/new 未返回 sessionId: {str(resp)[:300]}")
        return sid

    def prompt(self, session_id: str, text: str):
        return self.call("session/prompt", {
            "sessionId": session_id,
            "prompt": {"content": [{"type": "text", "text": text}]},
        })


def do_chat(page, message: str, agent_id=None, quiet=False) -> bool:
    """发起一次真实聊天(创建会话→沙盒→ACP 发送消息)。"""
    try:
        if not quiet:
            log(f"  建立会话沙盒{'(专家 ' + str(agent_id) + ')' if agent_id else ''} …")
        sess = create_sandbox_session(page, agent_id=agent_id)
        if not quiet:
            log("  连接 ACP …")
        acp = AcpClient(sess["link"], sess["token"])
        acp.initialize()
        sid = acp.new_session(sess["cwd"])
        if not quiet:
            log(f"  发送消息: {message[:50]}")
        resp = acp.prompt(sid, message)
        ok = "result" in resp or "error" not in resp
        if not quiet:
            preview = str(resp.get("result") or resp)[:120]
            log(f"  {'✓ 消息已送达' if ok else '✗ 发送失败'}: {preview}")
        return ok
    except Exception as e:  # noqa: BLE001
        log(f"  [警告] 聊天失败: {e}")
        return False


def list_agents(page):
    """列出云端专家/agents(用于专家类任务)。"""
    resp = api(page, "GET", API_AGENTS)
    agents = data_of(resp)
    if isinstance(agents, dict):
        agents = agents.get("agents") or agents.get("items") or []
    return agents or []


# ============================================================
# 兑换 / 抽奖 / 盲盒
# ============================================================

def redeem_streak(page):
    streak = data_of(api(page, "GET", API_STREAK))
    days = (streak.get("streak") or {}).get("days")
    status_all = streak.get("redemption_status") or {}
    if days is not None:
        nxt = status_all.get("remaining_days")
        log(f"  连续登录: {days} 天" + (f"(距下一档 {nxt} 天)" if nxt else ""))
    claimed = 0
    for tier in ("7d", "14d", "28d"):
        if status_all.get(f"tier_{tier}_status") == "claimable":
            resp = api(page, "POST", API_REDEEM,
                       {"tier": tier, "client_token": f"u-{uuid.uuid4()}"})
            if resp.get("code") == 0:
                d = data_of(resp)
                log(f"  ✓ 兑换 {tier} 档: +⚡{d.get('credit_granted', 0)} "
                    f"+◇{d.get('energy_granted', 0)} "
                    f"补登卡×{d.get('cards_granted', 0)} "
                    f"抽奖机会×{d.get('chances_granted', 0)}")
                claimed += 1
            else:
                log(f"  兑换 {tier} 失败: {resp.get('msg')}")
    if not claimed:
        log("  当前没有可兑换的连续登录奖励(7/14/28 天各一档)")
    return claimed


def draw_lottery(page):
    chances = data_of(api(page, "GET", API_LOTTERY_CHANCES)).get("chances") or 0
    if chances <= 0:
        log(f"  抽奖机会: 0,跳过")
        return 0
    drawn = 0
    for _ in range(chances):
        resp = api(page, "POST", API_LOTTERY_DRAW, {"client_token": f"u-{uuid.uuid4()}"})
        if resp.get("code") == 0:
            prize = data_of(resp)
            name = prize.get("prize_name") or prize.get("name") or json.dumps(prize, ensure_ascii=False)[:80]
            log(f"  ✓ 抽奖获得: {name}")
            drawn += 1
        else:
            log(f"  抽奖失败: {resp.get('msg')}")
        time.sleep(1)
    return drawn


def open_gacha(page):
    info = data_of(api(page, "GET", API_BUDDY_INFO))
    energy = data_of(api(page, "GET", API_ENERGY)).get("balance") or 0
    if energy < 10:
        log(f"  能量 {energy} < 10,无法开盲盒")
        return 0
    opened = 0
    while energy >= 10:
        resp = api(page, "POST", "/activity/growth/buddy/open", {"count": 1})
        if resp.get("code") != 0:
            log(f"  开盲盒失败: {resp.get('msg')}")
            break
        results = data_of(resp).get("results") or []
        for r in results:
            log(f"  ✓ 盲盒: {r.get('name', '?')} ({r.get('rarity', '?')})")
        opened += 1
        energy -= 10
        time.sleep(1)
    return opened


# ============================================================
# 主流程
# ============================================================

CHAT_TASK_CODE = "chat_5"          # 和 AI 聊天 5 次
EXPERT_TASK_CODE = "expert_5"      # 召唤 5 次专家
EXPERT_TEAM_TASK_CODE = "Expert_team_use_3"  # 召唤 3 次专家团


def run_automation(page, do_gacha=False, try_chat=False):
    log("== 第 1 步:接受所有任务 ==")
    accept_all(page)

    if try_chat:
        log("")
        log("== 第 2 步(实验):尝试自动完成聊天类任务 ==")
        log("  说明:纯 API 会话的云沙盒可能无法启动(服务端限制),失败属正常。")
        tasks = {t.get("task_code"): t for t in fetch_tasks(page)}
        t = tasks.get(CHAT_TASK_CODE)
        if t and t.get("accept_status") not in ("claimed", "completed"):
            prog = t.get("progress") or {}
            need = (prog.get("target") or 5) - (prog.get("current") or 0)
            done = 0
            for i in range(max(0, need)):
                log(f"  [chat_5] 第 {i + 1}/{need} 次对话…")
                if do_chat(page, f"你好,这是一次打卡对话({i + 1})。请回复:收到。"):
                    done += 1
                time.sleep(3)
                if i == 0 and done == 0:
                    log("  首次尝试失败,跳过剩余(沙盒不可用)")
                    break

    log("")
    log("== 第 2 步:领取已完成任务奖励 ==")
    claim_all(page)

    log("")
    log("== 第 3 步:兑换连续登录奖励 ==")
    redeem_streak(page)

    log("")
    log("== 第 4 步:抽奖 ==")
    draw_lottery(page)

    if do_gacha:
        log("")
        log("== 第 5 步:开 Buddy 盲盒 ==")
        open_gacha(page)

    log("")
    log("== 最新状态 ==")
    show_status(page)
    log("提示:产品体验类任务(聊天/画布/专家/技能/模板等)在 WorkBuddy 客户端正常使用即累计,")
    log("      完成后运行 python3 growth.py run 自动领奖。")


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 成长中心任务自动化工具")
    ap.add_argument("command", choices=["login", "status", "run", "chat",
                                         "accept", "claim", "redeem",
                                         "lottery", "gacha"],
                    help="login=登录 | status=查看 | run=一键自动化 | chat=手动聊天 | "
                         "accept/claim=接受/领奖 | redeem=连续登录兑换 | lottery=抽奖 | gacha=盲盒")
    ap.add_argument("message", nargs="?", help="chat 命令的消息内容")
    ap.add_argument("--cookies", help="login:导入 Cookie JSON 文件(浏览器插件导出格式)")
    ap.add_argument("--gacha", action="store_true", help="run:同时开 Buddy 盲盒")
    ap.add_argument("--try-chat", action="store_true",
                    help="run:尝试 API 自动聊天(实验,云沙盒受限时可能失败)")
    args = ap.parse_args()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    if args.command == "login":
        with sync_playwright() as pw:
            ctx = launch(pw, visible=True)
            if args.cookies:
                n = import_cookies(ctx, args.cookies)
                log(f"[登录] 已导入 {n} 条 workbuddy.cn Cookie")
                # 保存引导文件(会话自愈用),权限 600
                SESSION_FILE.write_text(
                    Path(args.cookies).read_text(encoding="utf-8"), encoding="utf-8")
                SESSION_FILE.chmod(0o600)
                # 立即用轻量页面验证
                page = ctx.new_page()
                page.goto(f"{ORIGIN}/robots.txt", wait_until="commit", timeout=60_000)
                page.wait_for_timeout(1500)
                if is_logged_in(page):
                    log("[登录] Cookie 有效!会话已保存,之后可直接 status / run。")
                else:
                    log("[错误] Cookie 无效或已过期,请重新导出,或直接扫码登录。")
                page.wait_for_timeout(3000)
                ctx.close()
                return
            log(f"[登录] 浏览器窗口已打开(会话目录: {PROFILE_DIR})")
            page = open_growth_page(ctx, wait_login=True)
            log("[登录] 登录成功!会话已持久保存,之后可直接 status / run。")
            log("[登录] 8 秒后自动关闭窗口…")
            page.wait_for_timeout(8000)
            ctx.close()
        return

    with sync_playwright() as pw:
        ctx = launch(pw, visible=False)   # 有头模式+窗口移屏幕外(WAF 拒绝无头)
        try:
            page = open_growth_page(ctx)
            if args.command == "status":
                show_status(page)
            elif args.command == "accept":
                accept_all(page)
            elif args.command == "claim":
                claim_all(page)
            elif args.command == "redeem":
                redeem_streak(page)
            elif args.command == "lottery":
                draw_lottery(page)
            elif args.command == "gacha":
                open_gacha(page)
            elif args.command == "chat":
                msg = args.message or "你好"
                do_chat(page, msg)
            elif args.command == "run":
                run_automation(page, do_gacha=args.gacha, try_chat=args.try_chat)
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
