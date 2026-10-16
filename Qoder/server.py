#!/usr/bin/env python3
"""Qoder 切号器 - 本地图形控制台

托管 index.html 并把页面操作桥接到 qoder-switch.sh。
仅监听 127.0.0.1,不对外网暴露;所有动作仍由 qoder-switch.sh 执行(单一逻辑源)。

用法:
  python3 server.py [--port 8790] [--no-open]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
AUTH_DIR = HOME / "Library/Application Support/com.qoder.app.stable"
AUTH_FILE = AUTH_DIR / "auth.v1.dat"
STATUS_FILE = HOME / ".qoder/.qoder-app-status.json"
SNAP_DIR = HOME / ".qoder/account-switcher-snapshots"
HERE = Path(__file__).resolve().parent

# 优先用 ~/.qoder 下的原件,其次用本目录副本
SCRIPT_CANDIDATES = [HOME / ".qoder/qoder-switch.sh", HERE / "qoder-switch.sh"]
SCRIPT = next((p for p in SCRIPT_CANDIDATES if p.is_file()), None)

# 快照名白名单:字母/数字/下划线/连字符/中文,1~32 位,禁止路径字符
NAME_RE = re.compile(r"^[\w\u4e00-\u9fa5][\w\u4e00-\u9fa5-]{0,31}$", re.UNICODE)


def read_status():
    """读取明文状态文件 ~/.qoder/.qoder-app-status.json(登录与应用启动时由主进程刷新)。"""
    try:
        d = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        uuid = "?"
        m = re.search(r"/users/([0-9a-f-]{36})/", d.get("avatar_url") or "")
        if m:
            uuid = m.group(1)
        return {
            "loggedIn": bool(d.get("logged_in")),
            "name": d.get("name") or "?",
            "email": d.get("email") or "?",
            "uuid": uuid,
            "snapshotAt": d.get("snapshot_at") or "",
        }
    except Exception:
        return None


def app_running():
    try:
        r = subprocess.run(["pgrep", "-x", "Qoder"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def read_meta(snap_dir):
    try:
        return json.loads((snap_dir / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_state():
    current = read_status()
    snapshots = []
    if SNAP_DIR.is_dir():
        for d in sorted(p for p in SNAP_DIR.iterdir() if p.is_dir()):
            if not (d / "auth.v1.dat").is_file():
                continue
            meta = read_meta(d)
            try:
                mtime = int((d / "auth.v1.dat").stat().st_mtime)
            except OSError:
                mtime = 0
            snapshots.append({
                "name": d.name,
                "accountName": meta.get("accountName", "?"),
                "email": meta.get("email", "?"),
                "uuid": meta.get("accountUuid", "?"),
                "mtime": mtime,
                "active": bool(current and current["uuid"] not in ("?",)
                               and meta.get("accountUuid") == current["uuid"]),
            })
    return {
        "current": current,
        "appRunning": app_running(),
        "authFilePresent": AUTH_FILE.is_file(),
        "scriptFound": SCRIPT is not None,
        "scriptPath": str(SCRIPT) if SCRIPT else "",
        "snapDir": str(SNAP_DIR),
        "snapshots": snapshots,
    }


def run_script(*args, force=False):
    """调用 qoder-switch.sh 并合并 stdout/stderr 返回。"""
    if SCRIPT is None:
        return {"ok": False, "output": "未找到 qoder-switch.sh(已尝试 ~/.qoder/ 与脚本同目录)"}
    env = dict(os.environ)
    if force:
        env["FORCE"] = "1"
    try:
        proc = subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True, text=True, timeout=120, env=env,
        )
        return {"ok": proc.returncode == 0,
                "output": (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "脚本执行超时(120s)"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "output": f"执行失败: {e}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "Qoder-Switch-Console/1.0"

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
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}") if length else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _valid_name(name):
        return isinstance(name, str) and bool(NAME_RE.match(name))

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
            self._json(build_state())
        elif path == "/api/health":
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/save":
            body = self._read_json_body()
            name = body.get("name", "")
            if not self._valid_name(name):
                self._json({"ok": False, "output": "无效的快照名:仅允许中文/字母/数字/下划线/连字符,1~32 位"})
                return
            self._json(run_script("save", name, force=bool(body.get("force"))))
        elif path == "/api/switch":
            body = self._read_json_body()
            name = body.get("name", "")
            if not self._valid_name(name):
                self._json({"ok": False, "output": "无效的快照名"})
                return
            snap = SNAP_DIR / name
            if not (snap / "auth.v1.dat").is_file():
                self._json({"ok": False, "output": f"无此快照: {snap}"})
                return
            self._json(run_script("switch", name))
        elif path == "/api/restart":
            self._json(run_script("restart"))
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def log_message(self, fmt, *args):  # 精简请求日志
        sys.stderr.write("[console] %s\n" % (fmt % args))


def main():
    ap = argparse.ArgumentParser(description="Qoder 切号器本地控制台")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    if SCRIPT is None:
        print("[警告] 未找到 qoder-switch.sh,页面可浏览但无法执行动作", file=sys.stderr)

    url = f"http://127.0.0.1:{args.port}"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"[错误] 端口 {args.port} 无法监听({e}),试试 --port 换一个")
    print(f"Qoder 切号器控制台已启动: {url}  (Ctrl+C 退出)")
    print(f"  脚本: {SCRIPT or '未找到'}")
    print(f"  快照目录: {SNAP_DIR}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
