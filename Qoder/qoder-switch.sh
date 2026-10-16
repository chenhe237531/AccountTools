#!/usr/bin/env bash
# Qoder 快速切号器 —— 基于 0.4.8 逆向分析 + 本机动态验证
#
# 原理:
#   登录凭据整体存于 auth.v1.dat(Electron safeStorage v10 加密,密钥在本机钥匙串),
#   应用启动时从该文件恢复登录态("Auth state restoration")。
#   外部替换该文件无热加载(已实测),因此切换 = 退出应用 → 原子换文件 → 重启应用。
#
# 会话文件: ~/Library/Application Support/com.qoder.app.stable/auth.v1.dat
# 伴生文件: auth-profile-overlays.v1.dat(同加密,登录时随账号一起更新)
# 账号识别: ~/.qoder/.qoder-app-status.json(明文,每次登录/启动刷新,含 name/email/UUID)
set -euo pipefail

AUTH_DIR="$HOME/Library/Application Support/com.qoder.app.stable"
AUTH_FILE="$AUTH_DIR/auth.v1.dat"
OVERLAYS_FILE="$AUTH_DIR/auth-profile-overlays.v1.dat"
STATUS_FILE="$HOME/.qoder/.qoder-app-status.json"
SNAP_DIR="$HOME/.qoder/account-switcher-snapshots"
APP_NAME="Qoder"

mkdir -p "$SNAP_DIR"; chmod 700 "$SNAP_DIR"

# ---------- 工具函数 ----------

stat_field() {  # stat_field <file> <py-expr over d>
  python3 - "$1" "$2" <<'PY'
import json,sys,re
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    print("?"); raise SystemExit
expr=sys.argv[2]
try:
    v=eval(expr,{"d":d,"re":re})
except Exception:
    v="?"
print(v if v not in (None,"") else "?")
PY
}

# 当前账号指纹: <uuid>\t<name>\t<email> (来自明文状态文件)
account_of_status() {
  python3 - "$1" <<'PY'
import json,re,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    print("?\t?\t?"); raise SystemExit
uuid="?"
m=re.search(r"/users/([0-9a-f-]{36})/",d.get("avatar_url") or "")
if m: uuid=m.group(1)
print(f"{uuid}\t{d.get('name') or '?'}\t{d.get('email') or '?'}")
PY
}

cur_uuid()   { [[ -f "$STATUS_FILE" ]] && account_of_status "$STATUS_FILE" | cut -f1 || echo "?"; }
cur_name()   { [[ -f "$STATUS_FILE" ]] && account_of_status "$STATUS_FILE" | cut -f2 || echo "?"; }
cur_email()  { [[ -f "$STATUS_FILE" ]] && account_of_status "$STATUS_FILE" | cut -f3 || echo "?"; }
logged_in()  { [[ -f "$STATUS_FILE" ]] && stat_field "$STATUS_FILE" "d.get('logged_in')" || echo "?"; }
logged_in_str() { [[ -f "$STATUS_FILE" ]] && stat_field "$STATUS_FILE" "'是' if d.get('logged_in') else '否'" || echo "?"; }

snap_uuid()  { [[ -f "$1/meta.json" ]] && stat_field "$1/meta.json" "d.get('accountUuid','?')" || echo "?"; }
snap_email() { [[ -f "$1/meta.json" ]] && stat_field "$1/meta.json" "d.get('email','?')" || echo "?"; }
snap_name()  { [[ -f "$1/meta.json" ]] && stat_field "$1/meta.json" "d.get('accountName','?')" || echo "?"; }

atomic_put() {  # atomic_put <src> <dst>  临时文件 + rename,避免写一半被读到
  local tmp; tmp="$(dirname "$2")/.qoder-switch.$$.tmp"
  cp "$1" "$tmp"; chmod 600 "$tmp"; mv -f "$tmp" "$2"
}

app_running() { pgrep -x "$APP_NAME" >/dev/null 2>&1; }

quit_app() {
  osascript -e "quit app \"$APP_NAME\"" 2>/dev/null || true
  local i=0
  while app_running && (( i < 30 )); do sleep 0.5; i=$((i+1)); done
  if app_running; then echo "警告: Qoder 未能在 15s 内退出,继续切换(可能有竞争风险)"; return 1; fi
  return 0
}

# 保存当前登录态到快照目录(写 meta + 凭据文件)
write_snapshot() {  # write_snapshot <dir>
  local dir="$1"
  mkdir -p "$dir"; chmod 700 "$dir"
  [[ -f "$AUTH_FILE" ]] || { echo "未找到会话文件 $AUTH_FILE"; return 1; }
  atomic_put "$AUTH_FILE" "$dir/auth.v1.dat"
  [[ -f "$OVERLAYS_FILE" ]] && atomic_put "$OVERLAYS_FILE" "$dir/auth-profile-overlays.v1.dat"
  local sha; sha=$(shasum -a 256 "$AUTH_FILE" | cut -c1-16)
  python3 - "$dir/meta.json" "$sha" <<'PY'
import json,sys,os,time,re
status=os.path.expanduser("~/.qoder/.qoder-app-status.json")
name=email=uuid="?"
try:
    d=json.load(open(status))
    name=d.get("name") or "?"; email=d.get("email") or "?"
    m=re.search(r"/users/([0-9a-f-]{36})/",d.get("avatar_url") or "")
    if m: uuid=m.group(1)
except Exception: pass
meta={"accountName":name,"email":email,"accountUuid":uuid,
      "savedAt":int(time.time()),
      "sourceFile":"~/Library/Application Support/com.qoder.app.stable/auth.v1.dat",
      "authSha256":sys.argv[2]}
p=sys.argv[1]
with open(os.path.dirname(p)+"/.meta.tmp","w") as f: json.dump(meta,f,ensure_ascii=False,indent=2)
os.replace(os.path.dirname(p)+"/.meta.tmp",p)
os.chmod(p,0o600)
PY
}

# ---------- 命令 ----------

case "${1:-}" in
  save)  # 当前登录账号存为快照
    [[ -f "$AUTH_FILE" ]] || { echo "未找到会话文件,请先在 Qoder 中登录一次"; exit 1; }
    name="${2:?用法: qoder-switch save <名字>}"
    dir="$SNAP_DIR/$name"
    if [[ -d "$dir" && -f "$dir/auth.v1.dat" && "${FORCE:-0}" != 1 ]]; then
      echo "快照已存在: $dir (FORCE=1 覆盖)"; exit 1
    fi
    [[ "$(logged_in)" == "False" ]] && echo "注意: 状态文件显示当前未登录,快照可能无效"
    write_snapshot "$dir" || exit 1
    echo "已保存 [$name] $(cur_name) <$(cur_email)> uuid=$(cur_uuid)";;

  switch)  # 退出应用 → 原子换文件 → 重启,启动时恢复目标账号
    name="${2:?用法: qoder-switch switch <名字>}"
    dir="$SNAP_DIR/$name"
    [[ -f "$dir/auth.v1.dat" ]] || { echo "无此快照: $dir"; ls "$SNAP_DIR" 2>/dev/null; exit 1; }
    tgt_uuid="$(snap_uuid "$dir")"
    cur="$(cur_uuid)"
    if [[ "$cur" != "?" && "$cur" != "None" && "$cur" == "$tgt_uuid" && "${FORCE:-0}" != 1 ]]; then
      echo "当前已是该账号 (uuid=$cur)。如需强制重换文件: FORCE=1 qoder-switch switch $name"; exit 0
    fi
    # 回存当前账号: 按 uuid 匹配既有快照,防止切换丢失当前登录态
    if [[ -f "$AUTH_FILE" && "$cur" != "?" && "$cur" != "None" && "$cur" != "$tgt_uuid" ]]; then
      backed=""
      for d2 in "$SNAP_DIR"/*/; do
        [[ -f "$d2/auth.v1.dat" ]] || continue
        if [[ "$(snap_uuid "$d2")" == "$cur" ]]; then
          write_snapshot "$d2" && backed="$(basename "$d2")"
          break
        fi
      done
      if [[ -z "$backed" && "${FORCE:-0}" != 1 ]]; then
        echo "当前账号 ($(cur_name) <$(cur_email)>) 尚无快照,直接切换会丢失其登录态。"
        echo "先执行: qoder-switch save <名字> 保存当前账号;或 FORCE=1 放弃当前登录态。"
        exit 1
      fi
      [[ -n "$backed" ]] && echo "已回存当前账号 → [$backed]"
    fi
    echo "正在退出 Qoder…"
    quit_app || true
    atomic_put "$dir/auth.v1.dat" "$AUTH_FILE"
    rm -f "$OVERLAYS_FILE"   # 先清,再按快照恢复(快照无 overlays 则保持无)
    [[ -f "$dir/auth-profile-overlays.v1.dat" ]] && atomic_put "$dir/auth-profile-overlays.v1.dat" "$OVERLAYS_FILE"
    open -a "$APP_NAME"
    echo "已切换到 [$name] $(snap_name "$dir") <$(snap_email "$dir")>"
    echo "应用启动时会自动恢复该账号登录态(~/.qoder/.qoder-app-status.json 随之刷新)";;

  list)
    echo "Qoder 运行中: $(app_running && echo 是 || echo 否) · 当前账号: $(cur_name) <$(cur_email)> · 已登录: $(logged_in_str)"
    for d in "$SNAP_DIR"/*/; do
      [[ -f "$d/auth.v1.dat" ]] || continue
      n="$(basename "$d")"
      printf '%-20s %-24s %-32s (%s)\n' "$n" "$(snap_name "$d")" "$(snap_email "$d")" "$(date -r "$d/auth.v1.dat" '+%m-%d %H:%M')"
    done;;

  current)
    echo "应用运行中 : $(app_running && echo 是 || echo 否)"
    echo "登录状态   : $(logged_in_str)"
    echo "账号       : $(cur_name) <$(cur_email)>"
    echo "账号 UUID  : $(cur_uuid)"
    echo "会话文件   : $AUTH_FILE ($(shasum -a 256 "$AUTH_FILE" 2>/dev/null | cut -c1-16 || echo 不存在))";;

  restart)  # 兜底: 重启应用(登录态从 auth.v1.dat 恢复,不会丢)
    echo "正在重启 Qoder…"
    quit_app || true
    open -a "$APP_NAME"
    echo "已重启 Qoder";;

  *)
    cat <<'EOF'
Qoder 快速切号器 (基于 0.4.8 逆向分析)

用法:
  qoder-switch save <名字>      把当前登录账号存为快照(凭据文件+账号指纹)
  qoder-switch switch <名字>    退出应用→换凭据文件→重启,自动恢复目标账号
  qoder-switch list             列出快照与当前账号
  qoder-switch current          显示当前登录状态
  qoder-switch restart          重启 Qoder(登录态不丢)

说明:
  - 切换前会把当前账号按 UUID 自动回存到既有快照(无快照且未 FORCE=1 时会拒绝切换)
  - 快照即凭据(含 refresh token),权限 600,切勿外传;加密绑定本机钥匙串,不可跨机器使用
  - 长期不用的账号建议定期切过去一次,保持 token 活性
EOF
    ;;
esac
