#!/usr/bin/env python3
"""WorkBuddy 桌面端自动化 —— 操作真实客户端完成成长任务,获取积分

原理:
  WorkBuddy 桌面端是 Electron(Chromium) 应用。本脚本:
  1. 以 --remote-debugging-port 重启应用(用户已知晓;与 wb-switch.sh restart 同模式);
  2. 通过 CDP(Chrome DevTools Protocol) 接管真实 UI;
  3. 驱动界面完成成长任务要求的真实操作(聊天/专家/画布/模板…,服务端按真实使用计数);
  4. 动作完成后调用 growth.py 的领奖逻辑收割积分与能量。

用法:
  python3 desktop.py ensure       # 确保应用带调试端口运行(必要时自动重启)
  python3 desktop.py status       # 查看成长任务进度(网页会话)
  python3 desktop.py explore      # dump 主窗口可交互元素(调试/适配用)
  python3 desktop.py screenshot   # 主窗口截图
  python3 desktop.py chat [N]     # 自动进行 N 次真实对话(默认 5,任务 chat_5)
  python3 desktop.py run          # 尽可能完成全部任务并领奖

依赖: pip install playwright requests ;应用为 /Applications/WorkBuddy.app
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

APP_NAME = "WorkBuddy"
CDP_PORT = 9223
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
HERE = Path(__file__).resolve().parent


def log(msg: str):
    print(msg, flush=True)


# ============================================================
# CDP 端口管理
# ============================================================

def cdp_alive() -> bool:
    try:
        r = requests.get(f"{CDP_URL}/json/version", timeout=3)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def ensure_cdp(restart: bool = True) -> bool:
    """确保 WorkBuddy 以调试端口运行。返回是否可用。"""
    if cdp_alive():
        log(f"[CDP] 调试端口已就绪: {CDP_URL}")
        return True
    if not restart:
        return False
    log("[CDP] 端口未开,带 --remote-debugging-port 重启 WorkBuddy …")
    subprocess.run(["osascript", "-e", f'quit app "{APP_NAME}"'],
                   capture_output=True, timeout=15)
    time.sleep(2)
    subprocess.run(["open", "-a", APP_NAME, "--args",
                    f"--remote-debugging-port={CDP_PORT}"], check=True)
    for _ in range(30):
        time.sleep(1)
        if cdp_alive():
            log(f"[CDP] 已就绪(等待 {'' if _ == 0 else str(_)} 秒)")
            return True
    log("[CDP] 30 秒内未就绪,请手动检查应用是否正常启动")
    return False


# ============================================================
# 连接与窗口
# ============================================================

def connect(pw):
    browser = pw.chromium.connect_over_cdp(CDP_URL)
    return browser


def list_pages(browser):
    pages = []
    for ctx in browser.contexts:
        for p in ctx.pages:
            try:
                pages.append({"page": p, "url": p.url, "title": ""})
            except Exception:  # noqa: BLE001
                pass
    # 补充标题
    for item in pages:
        try:
            item["title"] = item["page"].title() or ""
        except Exception:  # noqa: BLE001
            item["title"] = "?"
    return pages


def main_window(browser):
    """定位主聊天窗口(排除 about:blank / 帮助页等)。"""
    pages = list_pages(browser)
    if not pages:
        raise RuntimeError("未找到任何窗口页面")
    # 优先选 URL 非 blank 的窗口
    for item in pages:
        if item["url"] not in ("about:blank", ""):
            return item["page"]
    return pages[0]["page"]


# ============================================================
# UI 探索(调试/适配)
# ============================================================

DUMP_JS = """
() => {
  const out = [];
  const seen = new Set();
  const sels = [
    'button', 'textarea', 'input', '[contenteditable="true"]', '[role="button"]',
    '[role="textbox"]', '[role="menuitem"]', '[role="tab"]', '[role="option"]',
    'a[href]', '[class*="btn"]', '[class*="input"]', '[class*="editor"]',
  ];
  for (const sel of sels) {
    document.querySelectorAll(sel).forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) return;           // 不可见元素跳过
      const text = (el.innerText || el.getAttribute('aria-label') ||
                    el.getAttribute('placeholder') || el.getAttribute('title') || '')
                    .trim().replace(/\\s+/g, ' ').slice(0, 60);
      const key = sel + '|' + text + '|' + Math.round(r.x) + ',' + Math.round(r.y);
      if (seen.has(key)) return;
      seen.add(key);
      out.push({tag: el.tagName.toLowerCase(), sel, text,
                pos: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                id: el.id || '', cls: String(el.className).slice(0, 80)});
    });
  }
  out.sort((a, b) => (a.pos[1] - b.pos[1]) || (a.pos[0] - b.pos[0]));
  return out;
}
"""


def explore(page, filter_text: str = ""):
    items = page.evaluate(DUMP_JS)
    log(f"共 {len(items)} 个可交互元素:")
    for it in items:
        line = (f"  [{it['tag']:12s}] ({it['pos'][0]:5d},{it['pos'][1]:5d}) "
                f"{it['text']!r:40s} id={it['id']!r} cls={it['cls'][:60]}")
        if not filter_text or filter_text in line:
            log(line)
    return items


def screenshot(page, name="desktop-main"):
    path = f"/tmp/{name}.png"
    page.screenshot(path=path)
    log(f"截图: {path}")


# ============================================================
# 任务动作(基于 UI,选择器在探索后填充)
# ============================================================

def wait_ready(page, ms=3000):
    page.wait_for_timeout(ms)


def find_by_text(page, texts, timeout=8000, exact=False):
    """按可见文本找元素(支持多个候选文本)。"""
    if isinstance(texts, str):
        texts = [texts]
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for t in texts:
            try:
                loc = page.get_by_text(t, exact=exact).first
                if loc.is_visible(timeout=300):
                    return loc
            except Exception:  # noqa: BLE001
                continue
        page.wait_for_timeout(400)
    return None


# ============================================================
# 聊天(chat_5 / RichMeow_Chat 类任务)
# ============================================================

def send_message(page, text: str, wait_done: bool = True) -> bool:
    """在当前会话输入并发送一条真实消息,等待 AI 回复完成。"""
    try:
        editor = page.locator(".cr-input-editor-host").first
        editor.click(timeout=8000)
        page.wait_for_timeout(400)
        page.keyboard.type(text, delay=25)
        page.wait_for_timeout(300)
        send = page.locator(".cr-input-toolbar__send button").first
        send.click(timeout=5000)
    except Exception as e:  # noqa: BLE001
        log(f"  [发送失败] {e}")
        return False
    if wait_done:
        wait_response_done(page)
    return True


def wait_response_done(page, timeout: int = 120):
    """等待 AI 回复完成:最新回复出现"共消耗"(token 统计)即视为完成。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        done = page.evaluate("""() => {
            const marks = [...document.querySelectorAll('*')]
              .filter(el => el.children.length === 0 &&
                           el.innerText && el.innerText.includes('共消耗'));
            return marks.length > 0;
        }""")
        if done:
            return True
    log("  [警告] 等待回复超时,继续执行")
    return False


def new_conversation(page):
    """点击"新建任务"回到新会话输入页。"""
    try:
        btn = page.get_by_text("新建任务", exact=True).first
        btn.click(timeout=5000)
        page.wait_for_timeout(1500)
        return True
    except Exception:  # noqa: BLE001
        return False


def do_chat_task(page, total: int = 5):
    """完成 chat_5:进行 total 次真实对话(同一会话连发即可计数)。"""
    log(f"== 聊天任务:目标 {total} 次对话 ==")
    prompts = [
        "你好,请只回复:收到",
        "用一句话介绍你自己",
        "1+1等于几?只回答数字",
        "今天天气不错,说句吉利话",
        "请回复:好的,再见",
        "讲一个五十字以内的小笑话",
    ]
    ok = 0
    for i in range(total):
        msg = prompts[i % len(prompts)]
        log(f"  [{i + 1}/{total}] 发送: {msg}")
        if send_message(page, msg):
            ok += 1
            log(f"  [{i + 1}/{total}] ✓ 回复完成")
        time.sleep(2)
    log(f"  聊天完成 {ok}/{total}")
    return ok


# ============================================================
# 主流程
# ============================================================

def do_status():
    """查看成长任务进度(复用 growth.py 的网页会话)。"""
    subprocess.run([sys.executable, str(HERE / "growth.py"), "status"], check=False)


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 桌面端自动化(成长任务)")
    ap.add_argument("command", choices=["ensure", "status", "explore",
                                        "screenshot", "chat", "run"],
                    help="ensure=确保调试端口 | status=任务进度 | explore=UI探索 | "
                         "screenshot=截图 | chat=自动对话 | run=全自动完成任务")
    ap.add_argument("arg", nargs="?", help="chat 的次数 / explore 的过滤词")
    args = ap.parse_args()

    if args.command == "status":
        do_status()
        return

    if not ensure_cdp():
        sys.exit(1)

    with sync_playwright() as pw:
        browser = connect(pw)
        log(f"[CDP] 已连接,共 {len(browser.contexts)} 个 context")
        page = main_window(browser)
        log(f"[窗口] {page.url[:80]}")

        if args.command == "explore":
            explore(page, args.arg or "")
        elif args.command == "screenshot":
            wait_ready(page)
            screenshot(page)
        elif args.command == "chat":
            n = int(args.arg or 5)
            do_chat_task(page, n)
        elif args.command == "run":
            log("[run] 全任务自动化(逐步实现中)")
            do_chat_task(page, 5)


if __name__ == "__main__":
    main()
