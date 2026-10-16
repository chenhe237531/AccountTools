#!/usr/bin/env python3
"""Cursor 切号器 - 本地图形控制台

托管 index.html 并把页面操作桥接到 cursor-switch.sh。
仅监听 127.0.0.1,不对外网暴露;所有动作仍由 cursor-switch.sh 执行(单一逻辑源)。

用法:
  python3 server.py [--port 8791] [--no-open]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
DB = HOME / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
SNAP_DIR = HOME / ".cursor/account-switcher-snapshots"
HERE = Path(__file__).resolve().parent

# 优先用安装位置原件,其次用本目录副本
SCRIPT_CANDIDATES = [HOME / ".cursor/cursor-switch.sh", HERE / "cursor-switch.sh"]
SCRIPT = next((p for p in SCRIPT_CANDIDATES if p.is_file()), None)

# 快照名白名单:字母/数字/下划线/连字符/中文,1~32 位,禁止路径字符
NAME_RE = re.compile(r"^[\w\u4e00-\u9fa5][\w\u4e00-\u9fa5-]{0,31}$", re.UNICODE)

TOKEN_TTL_DAYS = 60  # JWT exp 距签发约 60 天


def read_db_rows():
    """只读打开 state.vscdb,返回 cursorAuth/* 与 lastSignedInAuthId 行(应用运行中安全)。"""
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


def jwt_payload(tok):
    import base64
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}


def summarize(rows):
    """从凭据行提取展示信息(与脚本 meta_of 一致)。"""
    at = rows.get("cursorAuth/accessToken")
    if not at:
        return None
    pl = jwt_payload(at) if isinstance(at, str) else {}
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


def cursor_running():
    try:
        return subprocess.run(["pgrep", "-x", "Cursor"],
                               capture_output=True).returncode == 0
    except Exception:
        return False


def build_state():
    current = summarize(read_db_rows())
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
                "email": info.get("email", "?"),
                "displayName": info.get("displayName", ""),
                "membership": info.get("membership", "?"),
                "authId": info.get("authId", ""),
                "tokenExp": exp,
                "expDays": int((exp - time.time()) // 86400) if exp else None,
                "savedAt": info.get("savedAt", ""),
                "mtime": mtime,
                "active": bool(current and info.get("authId") == current.get("authId")),
                "error": info.get("error"),
            })
    return {
        "current": current,
        "snapshots": snapshots,
        "cursorRunning": cursor_running(),
        "scriptFound": SCRIPT is not None,
        "scriptPath": str(SCRIPT) if SCRIPT else "",
        "snapDir": str(SNAP_DIR),
        "dbPath": str(DB),
        "dbExists": DB.is_file(),
    }


def run_script(*args, force=False):
    """调用 cursor-switch.sh 并合并 stdout/stderr 返回。"""
    if SCRIPT is None:
        return {"ok": False,
                "output": "未找到 cursor-switch.sh(已尝试 ~/.cursor/ 与脚本同目录)"}
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
    server_version = "Cursor-Switch-Console/1.0"

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
            snap = SNAP_DIR / f"{name}.json"
            if not snap.is_file():
                self._json({"ok": False, "output": f"无此快照: {snap}"})
                return
            self._json(run_script("switch", name, force=bool(body.get("force"))))
        elif path == "/api/remove":
            body = self._read_json_body()
            name = body.get("name", "")
            if not self._valid_name(name):
                self._json({"ok": False, "output": "无效的快照名"})
                return
            self._json(run_script("remove", name))
        elif path == "/api/check":
            body = self._read_json_body()
            name = body.get("name")
            args = ("check", name, "--json") if name else ("check", "--json")
            result = run_script(*args)
            import json as _json
            checks = []
            try:
                checks = _json.loads(result["output"].strip().splitlines()[-1])
            except Exception:
                pass
            self._json({"ok": result["ok"], "output": result["output"], "checks": checks})
        elif path == "/api/signout-local":
            self._json(run_script("signout-local"))
        elif path == "/api/restart":
            self._json(run_script("restart"))
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def log_message(self, fmt, *args):  # 精简请求日志
        sys.stderr.write("[console] %s\n" % (fmt % args))


def main():
    ap = argparse.ArgumentParser(description="Cursor 切号器本地控制台")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    if SCRIPT is None:
        print("[警告] 未找到 cursor-switch.sh,页面可浏览但无法执行动作", file=sys.stderr)

    url = f"http://127.0.0.1:{args.port}"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"[错误] 端口 {args.port} 无法监听({e}),试试 --port 换一个")
    print(f"Cursor 切号器控制台已启动: {url}  (Ctrl+C 退出)")
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
