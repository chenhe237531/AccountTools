#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trae 切号器 —— 本地图形控制台

托管 index.html，并把页面操作桥接到 trae_switch.py。
仅监听 127.0.0.1，不对外网/局域网暴露；所有逻辑以 trae_switch.py 为唯一真相源。

用法:
  python3 server.py [--port 8788] [--no-open]
"""
import argparse
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import trae_switch as ts  # noqa: E402

PORT_DEFAULT = 8788


def _state(profile: str):
    try:
        return ts.build_state(profile)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "profile": profile,
                "profileLabel": ts.PROFILES.get(profile, {}).get("label", profile),
                "current": None, "snapshots": [], "running": False,
                "storagePath": "", "snapDir": ""}


class Handler(BaseHTTPRequestHandler):
    server_version = "Trae-Switch-Console/1.0"

    # ---------- helpers ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}") if n else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _query(path):
        q = {}
        if "?" in path:
            for pair in path.split("?", 1)[1].split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    q[k] = v
        return q

    @staticmethod
    def _profile(value):
        p = value or ts.DEFAULT_PROFILE
        return p if p in ts.PROFILES else ts.DEFAULT_PROFILE

    # ---------- routes ----------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            page = HERE / "index.html"
            if page.is_file():
                self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, "index.html not found", "text/plain; charset=utf-8")
        elif path == "/api/state":
            self._json(_state(self._profile(self._query(self.path).get("profile"))))
        elif path == "/api/health":
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._read_json_body()
        profile = self._profile(body.get("profile"))

        if path == "/api/save":
            name = (body.get("name") or "").strip()
            if not ts.NAME_RE.match(name):
                self._json({"ok": False, "output": "无效的快照名：仅允许中文/字母/数字/下划线/连字符，1~32 位"})
                return
            try:
                snap = ts.save_snapshot(profile, name, force=bool(body.get("force")))
                self._json({"ok": True, "output":
                            f"已保存快照「{name}」：{snap['username'] or '(未解析)'} "
                            f"uid={snap['userId']} 区域={snap['region']} 套餐={snap['plan'] or '—'}"})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "output": f"{type(e).__name__}: {e}"})

        elif path == "/api/switch":
            name = (body.get("name") or "").strip()
            if not ts.NAME_RE.match(name):
                self._json({"ok": False, "output": "无效的快照名"})
                return
            if ts.load_snapshot(profile, name) is None:
                self._json({"ok": False, "output": f"无此快照：{name}"})
                return
            try:
                res = ts.do_switch(profile, name, relaunch=bool(body.get("relaunch", True)))
                self._json({"ok": True, "output": "\n".join(res["steps"]), **res})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "output": f"{type(e).__name__}: {e}"})

        elif path == "/api/restart":
            try:
                res = ts.do_restart(profile)
                self._json({"ok": True, "output": "\n".join(res["steps"])})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "output": f"{type(e).__name__}: {e}"})

        elif path == "/api/delete":
            name = (body.get("name") or "").strip()
            if not ts.NAME_RE.match(name):
                self._json({"ok": False, "output": "无效的快照名"})
                return
            try:
                ts.delete_snapshot(profile, name)
                self._json({"ok": True, "output": f"已删除快照「{name}」"})
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "output": f"{type(e).__name__}: {e}"})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def log_message(self, fmt, *args):  # 精简请求日志
        sys.stderr.write("[console] %s\n" % (fmt % args))


def main():
    ap = argparse.ArgumentParser(description="Trae 切号器本地控制台")
    ap.add_argument("--port", type=int, default=PORT_DEFAULT)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"[错误] 端口 {args.port} 无法监听（{e}），试试 --port 换一个")
    print(f"Trae 切号器控制台已启动：{url}  (Ctrl+C 退出)")
    print(f"  脚本: {HERE / 'trae_switch.py'}")
    print(f"  快照: {ts.SNAP_ROOT}")
    print("  提示: 切换会先退出 Trae 再重启，请先保存好编辑器中的文件。")
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
