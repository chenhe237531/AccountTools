#!/usr/bin/env bash
# WorkBuddy 快速切号器 —— 基于 5.7.6 静态分析结论
# 会话文件: ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
# 原理: 整文件快照交换;应用自带 fs.watch 会热加载外部修改,通常无需重启
set -euo pipefail

AUTH_DIR="$HOME/Library/Application Support/CodeBuddyExtension/Data/Public/auth"
AUTH_FILE="$AUTH_DIR/workbuddy-desktop.info"
SNAP_DIR="$HOME/.workbuddy/account-switcher-snapshots"
MARKER="$AUTH_FILE.logged-out"

mkdir -p "$SNAP_DIR"; chmod 700 "$SNAP_DIR"

uid_of() { python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['account']['uid'][:8])" "$1" 2>/dev/null || echo "?"; }
uin_of() { python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['account'].get('uin','?'))" "$1" 2>/dev/null || echo "?"; }

atomic_put() {  # atomic_put <src> <dst>
  local tmp; tmp="$(dirname "$2")/.wb-switch.$$.tmp"
  cp "$1" "$tmp"; chmod 600 "$tmp"; mv -f "$tmp" "$2"
}

case "${1:-}" in
  save)   # 当前登录账号存为快照
    [[ -f "$AUTH_FILE" ]] || { echo "未找到会话文件,请先登录一次 WorkBuddy"; exit 1; }
    name="${2:?用法: wb-switch save <名字>}"
    out="$SNAP_DIR/$name.info"
    [[ -f "$out" && "${FORCE:-0}" != 1 ]] && { echo "快照已存在: $out (FORCE=1 覆盖)"; exit 1; }
    atomic_put "$AUTH_FILE" "$out"
    echo "已保存 [$name] uid=$(uid_of "$out") uin=$(uin_of "$out")";;

  switch)  # 原子替换会话文件,应用会热加载
    name="${2:?用法: wb-switch switch <名字>}"
    snap="$SNAP_DIR/$name.info"
    [[ -f "$snap" ]] || { echo "无此快照: $snap"; ls "$SNAP_DIR"/*.info 2>/dev/null | xargs -n1 basename 2>/dev/null; exit 1; }
    # 保存当前账号(若与目标不同)
    if [[ -f "$AUTH_FILE" ]]; then
      cur_uid=$(uid_of "$AUTH_FILE"); tgt_uid=$(uid_of "$snap")
      if [[ "$cur_uid" != "$tgt_uid" ]]; then
        for n in "$SNAP_DIR"/*.info; do
          [[ "$(uid_of "$n")" == "$cur_uid" ]] && { atomic_put "$AUTH_FILE" "$n"; echo "已回存当前账号 → $(basename "$n")"; break; }
        done
      fi
    fi
    rm -f "$MARKER"                      # 清掉可能存在的登出标记
    atomic_put "$snap" "$AUTH_FILE"      # tmp+rename 原子写
    echo "已切换到 [$name] uid=$(uid_of "$AUTH_FILE") —— 应用通常 1~2 秒内自动生效"
    echo "若未生效: wb-switch restart";;

  list)
    echo "当前: uid=$(uid_of "$AUTH_FILE" 2>/dev/null) uin=$(uin_of "$AUTH_FILE" 2>/dev/null)"
    for f in "$SNAP_DIR"/*.info; do
      [[ -f "$f" ]] || continue
      printf '%-24s uid=%s uin=%s  (%s)\n' "$(basename "$f" .info)" "$(uid_of "$f")" "$(uin_of "$f")" "$(date -r "$f" '+%m-%d %H:%M')"
    done;;

  restart)  # 兜底: 重启应用
    osascript -e 'quit app "WorkBuddy AI"' 2>/dev/null; sleep 1
    open -a "WorkBuddy AI"; echo "已重启 WorkBuddy";;

  *) cat <<'EOF'
用法:
  wb-switch save <名字>      把当前登录账号存为快照
  wb-switch switch <名字>    切换到指定账号(自动回存当前账号)
  wb-switch list             列出快照与当前账号
  wb-switch restart          重启 WorkBuddy(热加载未生效时用)
提示: refresh token 约 30 天过期,长期不用的账号记得定期切过去刷新一次。
EOF
    ;;
esac
