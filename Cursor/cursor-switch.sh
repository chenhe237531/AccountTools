#!/usr/bin/env bash
# Cursor 快速切号器 —— 基于 Cursor 3.21.9 / 3.24.18 静态分析结论
# 凭据位置: ~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
#           (SQLite ItemTable 中的 cursorAuth/* 键 + glass.lastSignedInAuthId,均明文 TEXT)
# 原理: 行级快照 —— 只搬运登录凭据行,不碰 700MB+ 的聊天/工作区数据;
#       Cursor 运行期间持有 SQLite 连接且不监听外部改动,故必须 退出 → 改库 → 重启。
set -euo pipefail

DB="$HOME/Library/Application Support/Cursor/User/globalStorage/state.vscdb"
SNAP_DIR="$HOME/.cursor/account-switcher-snapshots"
APP_NAME="Cursor"

mkdir -p "$SNAP_DIR"; chmod 700 "$SNAP_DIR"

# ---------- 内嵌 Python:所有 DB / JWT / 快照操作 ----------
cs_py() {
  python3 - "$@" <<'PY'
import sys, os, json, re, sqlite3, base64, time, glob

DB = os.path.expanduser("~/Library/Application Support/Cursor/User/globalStorage/state.vscdb")
SNAP_DIR = os.path.expanduser("~/.cursor/account-switcher-snapshots")
FORCE = os.environ.get("FORCE") == "1"
NAME_RE = re.compile(r"^[\w\u4e00-\u9fa5][\w\u4e00-\u9fa5-]{0,31}$", re.UNICODE)

AUTH_KEYS_PREFIX = "cursorAuth/"
LAST_AUTH_KEY = "glass.lastSignedInAuthId"

def valid_name(n):
    return bool(n) and bool(NAME_RE.match(n))

def snap_path(name):
    return os.path.join(SNAP_DIR, f"{name}.json")

def jwt_payload(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return {}

def connect(readonly=True):
    if readonly:
        return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    return sqlite3.connect(DB, timeout=5)

def read_rows(conn):
    rows = {}
    for k, v in conn.execute(
        "SELECT key, value FROM ItemTable WHERE key LIKE 'cursorAuth/%' OR key=?",
        (LAST_AUTH_KEY,)):
        rows[k] = v
    return rows

def enc(v):   # 快照里 bytes -> base64 标记, str 原样
    if isinstance(v, bytes):
        return "__base64__:" + base64.b64encode(v).decode()
    return v

def dec(v):   # 还原
    if isinstance(v, str) and v.startswith("__base64__:"):
        return base64.b64decode(v[11:])
    return v

def meta_of(rows):
    at = rows.get("cursorAuth/accessToken") or ""
    pl = jwt_payload(at) if isinstance(at, str) else {}
    email = rows.get("cursorAuth/cachedEmail") or ""
    disp = ""
    try:
        prof = rows.get("cursorAuth/cachedScopedProfile") or ""
        if isinstance(prof, str) and prof:
            disp = json.loads(prof).get("displayName") or ""
    except Exception:
        pass
    return {
        "authId": pl.get("sub") or (rows.get(LAST_AUTH_KEY) if rows.get(LAST_AUTH_KEY) not in (None, "signed-out", "") else ""),
        "email": email if isinstance(email, str) else "",
        "displayName": disp,
        "membership": rows.get("cursorAuth/stripeMembershipType") or "",
        "subscriptionStatus": rows.get("cursorAuth/stripeSubscriptionStatus") or "",
        "signUpType": rows.get("cursorAuth/cachedSignUpType") or "",
        "tokenExp": pl.get("exp"),
    }

def build_snapshot(rows, name=None):
    m = meta_of(rows)
    m.update({"version": 1, "app": "cursor",
              "savedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
              "savedAtEpoch": int(time.time()),
              "rows": {k: enc(v) for k, v in rows.items()}})
    if name:
        m["name"] = name
    return m

def atomic_write_json(path, obj):
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

def load_snapshot(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def fmt_dt(epoch):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
    except Exception:
        return "?"

def days_left(epoch):
    if not epoch:
        return None
    return int((epoch - time.time()) // 86400)

def err(msg):
    print(msg, file=sys.stderr); sys.exit(1)

def cmd_current():
    if not os.path.isfile(DB):
        err("未找到 state.vscdb —— 请确认 Cursor 已在本机初始化")
    with connect() as c:
        rows = read_rows(c)
    if "--json" in sys.argv:
        m = meta_of(rows); m["signedIn"] = bool(rows.get("cursorAuth/accessToken"))
        print(json.dumps(m, ensure_ascii=False)); return
    m = meta_of(rows)
    if not rows.get("cursorAuth/accessToken"):
        print("当前: 未登录"); return
    dl = days_left(m.get("tokenExp"))
    print(f"当前: {m['displayName'] or '(未命名)'} <{m['email'] or '?'}> [{m['membership'] or '?'}]")
    print(f"  authId: {m['authId']}  登录方式: {m['signUpType'] or '?'}")
    print(f"  token 到期: {fmt_dt(m.get('tokenExp'))}" + (f" (剩 {dl} 天)" if dl is not None else ""))

def cmd_save():
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    name = args[0] if args else ""
    if not valid_name(name):
        err("用法: cursor-switch save <名字>  (中文/字母/数字/下划线/连字符,1~32 位)")
    out = snap_path(name)
    if os.path.exists(out) and not FORCE:
        err(f"快照已存在: {out}  (FORCE=1 覆盖)")
    if not os.path.isfile(DB):
        err("未找到 state.vscdb")
    with connect() as c:
        rows = read_rows(c)
    if not rows.get("cursorAuth/accessToken"):
        err("当前未登录 Cursor,请先登录再保存快照")
    snap = build_snapshot(rows, name)
    atomic_write_json(out, snap)
    print(f"已保存 [{name}] {snap['displayName'] or ''} <{snap['email']}> authId={snap['authId']}")

def auto_save_current():
    """切换前自动回存当前账号:优先覆盖同 authId 快照,否则按邮箱命名新建。"""
    if not os.path.isfile(DB):
        return None
    with connect() as c:
        rows = read_rows(c)
    if not rows.get("cursorAuth/accessToken"):
        return None
    cur = meta_of(rows)
    # 1) 找同 authId 的既有快照
    for f in sorted(glob.glob(os.path.join(SNAP_DIR, "*.json"))):
        try:
            if load_snapshot(f).get("authId") == cur.get("authId"):
                name = os.path.basename(f)[:-5]
                atomic_write_json(f, build_snapshot(rows, name))
                return name
        except Exception:
            continue
    # 2) 新建 auto-<邮箱前缀>
    local = re.sub(r"[^\w\u4e00-\u9fa5-]", "", (cur.get("email") or "account").split("@")[0])[:20] or "account"
    name = f"auto-{local}"
    if not valid_name(name):
        name = "auto-account"
    atomic_write_json(snap_path(name), build_snapshot(rows, name))
    return name

def cmd_apply():
    path = sys.argv[2]
    if not os.path.isfile(path):
        err(f"无此快照文件: {path}")
    snap = load_snapshot(path)
    rows = snap.get("rows") or {}
    if not rows.get("cursorAuth/accessToken"):
        err("快照里没有 accessToken,疑似损坏")
    # 安全检查:Cursor 必须已退出
    import subprocess
    r = subprocess.run(["pgrep", "-x", "Cursor"], capture_output=True)
    if r.returncode == 0:
        err("Cursor 仍在运行,拒绝写入数据库 —— 请先用 cursor-switch restart/switch 流程退出应用")
    conn = sqlite3.connect(DB, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("DELETE FROM ItemTable WHERE key LIKE 'cursorAuth/%' OR key=?",
                     (LAST_AUTH_KEY,))
        for k, v in rows.items():
            conn.execute("INSERT OR REPLACE INTO ItemTable(key, value) VALUES(?, ?)",
                         (k, dec(v)))
        conn.commit()
    finally:
        conn.close()
    m = meta_of({k: dec(v) for k, v in rows.items()})
    print(f"已写入凭据: {m['displayName'] or ''} <{m['email']}> authId={m['authId']}")

def cmd_switch():
    """bash 侧完成退出/回存编排后调用:apply + 打印结果信息。"""
    path = sys.argv[2]
    cmd_apply()
    snap = load_snapshot(path)
    dl = days_left(snap.get("tokenExp"))
    print(f"目标账号 token 到期: {fmt_dt(snap.get('tokenExp'))}" + (f" (剩 {dl} 天)" if dl is not None else ""))

def cmd_autosave():
    n = auto_save_current()
    if n:
        print(f"已回存当前账号 → [{n}]")
    else:
        print("当前未登录,跳过回存")

def cmd_list():
    if os.path.isfile(DB):
        with connect() as c:
            rows = read_rows(c)
        m = meta_of(rows)
        if rows.get("cursorAuth/accessToken"):
            dl = days_left(m.get("tokenExp"))
            print(f"当前: {m['displayName'] or '(未命名)'} <{m['email'] or '?'}> [{m['membership'] or '?'}]")
            print(f"  authId: {m['authId']}  token 至 {fmt_dt(m.get('tokenExp'))}" + (f" (剩 {dl} 天)" if dl is not None else ""))
        else:
            print("当前: 未登录")
    else:
        print("当前: (未找到 state.vscdb)")
    print()
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "*.json")))
    if not files:
        print("快照: (空 —— 登录账号后执行 cursor-switch save <名字>)")
        return
    print("快照:")
    for f in files:
        try:
            s = load_snapshot(f)
        except Exception:
            print(f"  {os.path.basename(f)[:-5]:<20} (解析失败,可能损坏)")
            continue
        dl = days_left(s.get("tokenExp"))
        exp = "已过期?" if (dl is not None and dl <= 0) else (f"剩{dl}天" if dl is not None else "?")
        print(f"  {s.get('name') or os.path.basename(f)[:-5]:<20} "
              f"{(s.get('displayName') or '') + ' <' + (s.get('email') or '?') + '>':<44} "
              f"[{s.get('membership') or '?'}] token {exp:<7} 保存于 {s.get('savedAt', '?')}")

def cmd_meta():
    """读取某快照的 authId(供 bash 在退出应用前做同号检测)。"""
    snap = load_snapshot(sys.argv[2])
    print(json.dumps({"authId": snap.get("authId"), "email": snap.get("email"),
                      "displayName": snap.get("displayName")}, ensure_ascii=False))

def cmd_remove():
    name = sys.argv[2] if len(sys.argv) > 2 else ""
    if not valid_name(name):
        err("用法: cursor-switch remove <名字>")
    p = snap_path(name)
    if not os.path.isfile(p):
        err(f"无此快照: {p}")
    os.remove(p)
    print(f"已删除快照 [{name}]")

# ---------- token 存活检测(只读 initialize 握手,不改任何状态) ----------
def probe_token(tok):
    import urllib.request, urllib.error
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "cursor-switch", "version": "1.0"}}}).encode()
    req = urllib.request.Request(
        "https://api.origin.cursor.com/mcp", data=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Authorization": f"Bearer {tok}"}, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=12)
        r.read(64)
        return "valid", r.status
    except urllib.error.HTTPError as e:
        e.read(64)
        return ("invalid" if e.code in (401, 403) else "error"), e.code
    except Exception as e:
        return "error", str(e)[:60]

def cmd_check():
    """检测各快照(及当前账号)token 的服务端存活状态。--json 输出机器可读格式。"""
    as_json = "--json" in sys.argv
    targets = []  # (label, token, exp)
    if os.path.isfile(DB):
        with connect() as c:
            rows = read_rows(c)
        at = rows.get("cursorAuth/accessToken")
        if isinstance(at, str) and at:
            targets.append(("<当前账号>", at, meta_of(rows).get("tokenExp")))
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    if args:
        for name in args:
            p = snap_path(name)
            if not os.path.isfile(p):
                if not as_json: print(f"[{name}] 无此快照")
                continue
            s = load_snapshot(p)
            targets.append((s.get("name") or name, s["rows"].get("cursorAuth/accessToken", ""), s.get("tokenExp")))
    else:
        for f in sorted(glob.glob(os.path.join(SNAP_DIR, "*.json"))):
            try:
                s = load_snapshot(f)
                targets.append((s.get("name") or os.path.basename(f)[:-5],
                                s["rows"].get("cursorAuth/accessToken", ""), s.get("tokenExp")))
            except Exception:
                if not as_json: print(f"[{os.path.basename(f)[:-5]}] 快照损坏,无法检测")
    results = []
    for label, tok, exp in targets:
        if not tok:
            results.append({"name": label, "status": "no-token", "http": None, "expDays": None})
            continue
        status, http = probe_token(tok)
        dl = days_left(exp)
        results.append({"name": label, "status": status, "http": http, "expDays": dl})
    if as_json:
        print(json.dumps(results, ensure_ascii=False))
        return
    CN = {"valid": "有效 ✓", "invalid": "已失效 ✗ (服务端已吊销)", "error": "检测失败", "no-token": "无 token"}
    for r in results:
        line = f"[{r['name']}] {CN.get(r['status'], r['status'])}"
        if r.get("http") is not None:
            line += f" (HTTP {r['http']})"
        if r.get("expDays") is not None:
            line += f" · 距到期 {r['expDays']} 天"
        print(line)

def cmd_signout_local():
    """本地登出:清空凭据行但不调用服务端 /auth/logout —— 会话保持存活,可安全登录下一个账号。"""
    import subprocess
    r = subprocess.run(["pgrep", "-x", "Cursor"], capture_output=True)
    if r.returncode == 0:
        err("Cursor 仍在运行,拒绝写入数据库 —— signout-local 同样需要先退出应用")
    conn = sqlite3.connect(DB, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("DELETE FROM ItemTable WHERE key LIKE 'cursorAuth/%' OR key=?", (LAST_AUTH_KEY,))
        # 复刻真实登出态(实测自登出后的 DB)
        conn.execute("INSERT OR REPLACE INTO ItemTable(key, value) VALUES('cursorAuth/cachedEmail', '')")
        conn.execute("INSERT OR REPLACE INTO ItemTable(key, value) VALUES('cursorAuth/cachedSignUpType', 'unknown')")
        conn.execute("INSERT OR REPLACE INTO ItemTable(key, value) VALUES(?, 'signed-out')", (LAST_AUTH_KEY,))
        conn.commit()
    finally:
        conn.close()
    print("已本地登出(未通知服务器,原会话仍然存活)。")

# 快照文件里补写 name 字段,便于 list 展示(build_snapshot 的 name 参数)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "current": cmd_current()
    elif cmd == "save": cmd_save()
    elif cmd == "autosave": cmd_autosave()
    elif cmd == "apply": cmd_apply()
    elif cmd == "switch": cmd_switch()
    elif cmd == "list": cmd_list()
    elif cmd == "meta": cmd_meta()
    elif cmd == "remove": cmd_remove()
    elif cmd == "check": cmd_check()
    elif cmd == "signout-local": cmd_signout_local()
    else: err("未知子命令: " + (cmd or "(空)"))
PY
}

# ---------- 退出 / 重启 Cursor ----------
cursor_running() { pgrep -x Cursor >/dev/null 2>&1; }

quit_cursor() {
  if ! cursor_running; then echo "Cursor 未在运行"; return 0; fi
  echo "正在退出 Cursor(等待最多 15 秒)…"
  osascript -e 'quit app "Cursor"' >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    cursor_running || { echo "Cursor 已退出"; return 0; }
    sleep 0.5
  done
  echo "错误: Cursor 未在 15 秒内退出(可能有未保存文件的确认框)。" >&2
  echo "已中止,未对数据库做任何修改。请保存文件退出 Cursor 后重试。" >&2
  return 1
}

relaunch_cursor() {
  if [[ "${NO_RELAUNCH:-0}" == "1" ]]; then
    echo "NO_RELAUNCH=1:跳过重启,请手动 open -a Cursor"
    return 0
  fi
  open -a "$APP_NAME" && echo "已重新启动 Cursor"
}

# ---------- 主流程 ----------
case "${1:-}" in
  save)
    cs_py save "${2:-}"
    ;;

  switch)
    name="${2:?用法: cursor-switch switch <名字>}"
    snap="$SNAP_DIR/$name.json"
    [[ -f "$snap" ]] || { echo "无此快照: $snap"; cs_py list >/dev/null 2>&1 || true; exit 1; }

    # 目标账号信息
    tgt=$(cs_py meta "$snap")
    tgt_id=$(printf '%s' "$tgt" | python3 -c "import json,sys;print(json.load(sys.stdin).get('authId') or '')")
    tgt_name=$(printf '%s' "$tgt" | python3 -c "import json,sys;d=json.load(sys.stdin);print((d.get('displayName') or '')+' <'+(d.get('email') or '?')+'>')")

    # 同号快速通道:未运行 FORCE=1 时不折腾
    if [[ "${FORCE:-0}" != "1" ]] && [[ -f "$DB" ]]; then
      cur=$(cs_py current --json 2>/dev/null || echo '{}')
      cur_id=$(printf '%s' "$cur" | python3 -c "import json,sys;print(json.load(sys.stdin).get('authId') or '')" 2>/dev/null || true)
      if [[ -n "$cur_id" && "$cur_id" == "$tgt_id" ]]; then
        echo "当前已是该账号($tgt_name),无需切换。如需恢复快照中的 token 用: FORCE=1 cursor-switch switch $name"
        exit 0
      fi
    fi

    echo "目标账号: $tgt_name"
    quit_cursor                     # ① 退出(失败即中止,库未被碰)
    cs_py autosave                  # ② 回存当前账号(应用已关,读到最终状态)
    cs_py switch "$snap"            # ③ 行级替换 cursorAuth/* + lastSignedInAuthId
    relaunch_cursor                 # ④ 重启
    echo "完成。如界面未刷新登录态,等 5~10 秒或手动重启一次 Cursor。"
    ;;

  list)
    cs_py list
    ;;

  current)
    cs_py current
    ;;

  remove)
    name="${2:?用法: cursor-switch remove <名字>}"
    cs_py remove "$name"
    ;;

  check)
    cs_py check "${@:2}"
    ;;

  signout-local)
    echo "本地登出:只清本机凭据,不调用服务端 /auth/logout(当前账号会话保持存活)。"
    quit_cursor
    cs_py signout-local
    relaunch_cursor
    echo "完成。Cursor 现为未登录状态,可在设置里登录下一个账号,登录后用 save 建立快照。"
    echo "切记:多账号场景永远不要点 Cursor 界面里的 Sign Out —— 那会服务端吊销当前账号会话!"
    ;;

  restart)
    quit_cursor
    sleep 1
    relaunch_cursor
    ;;

  *)
    cat <<'EOF'
Cursor 快速切号器 v1.1

用法:
  cursor-switch save <名字>        把当前登录账号的凭据存为快照(应用可运行中,只读)
  cursor-switch switch <名字>      切换到指定账号(自动退出 Cursor → 回存当前 → 改库 → 重启)
  cursor-switch list               列出当前账号与所有快照
  cursor-switch current            仅显示当前登录账号
  cursor-switch check [名字...]    检测当前账号/快照 token 的服务端存活状态(只读)
  cursor-switch signout-local      本地登出:清凭据但不吊销会话,用于安全登录下一个账号
  cursor-switch remove <名字>      删除指定快照
  cursor-switch restart            重启 Cursor(不改动任何数据)

环境变量:
  FORCE=1         save 覆盖同名快照 / switch 强制切换(即使目标与当前同号,用于恢复快照 token)
  NO_RELAUNCH=1   switch / signout-local 后不自动重启(脚本化场景)

多账号初始化(重要!):
  1. Cursor 里登录账号 A → cursor-switch save A
  2. cursor-switch signout-local   ← 千万不要点界面里的 Sign Out(会服务端吊销 A 的会话)
  3. Cursor 里登录账号 B → cursor-switch save B
  4. 之后 switch A / switch B 自由往返

原理:
  Cursor 登录凭据 = state.vscdb(ItemTable)中的 cursorAuth/* 明文行 + glass.lastSignedInAuthId。
  本脚本只做这几行的快照与替换,不触碰聊天/工作区等大文件数据。
  应用运行时持有 SQLite 连接且不监听外部改动,因此切换必须经过 退出→改库→重启。
  UI 的 Sign Out 会 POST /auth/logout 服务端吊销 token;本脚本的所有操作均不通知服务器。

提示:
  token 有效期约 60 天,应用会自动续期;长期不用的账号记得定期 switch 过去一次再切回。
  切换后如遇 AI 功能报错,用 check 确认 token 是否被服务端吊销(通常因误点过 UI 登出)。
EOF
    ;;
esac
