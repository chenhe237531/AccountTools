"""WorkBuddy 适配器：账号快照（wb-switch.sh）+ wbipc 代签 + token 直连账号池
+ 成长任务自动化 + 凭证 JSON 导入。

核心模块（单一逻辑源，均位于 Workbuddy/）：
  wb-switch.sh   账号快照保存/切换（优先用 ~/.workbuddy/ 安装原件）
  wb_checkin.py  wbipc 客户端：让应用进程用当前会话调官方签到 API
  wb_growth.py   成长任务（wbipc 轻量通道 + 桌面端 CDP 通道）
  wb_import.py   token 直连账号池（~/.workbuddy/token-accounts.json，600 权限）
"""
from __future__ import annotations

import importlib.util
import json
import time

from app.agents import register
from app.agents.base import AgentAdapter, Feature
from app.core import jobs, logs, paths, proc


def _load_module(path, name):
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:  # noqa: BLE001
        import sys
        print(f"[警告] {name} 模块加载失败: {e}", file=sys.stderr)
        return None


WB_DIR = paths.ROOT / "Workbuddy"
WB_AUTH_FILE = paths.HOME / "Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
WB_SNAP_DIR = paths.HOME / ".workbuddy/account-switcher-snapshots"
WB_SCRIPT = next((p for p in [paths.HOME / ".workbuddy/wb-switch.sh", WB_DIR / "wb-switch.sh"]
                  if p.is_file()), None)

_wbi = _load_module(WB_DIR / "wb_import.py", "wb_import")      # token 直连账号池
_wg = _load_module(WB_DIR / "wb_growth.py", "wb_growth")       # 成长任务


def _wb_read_account(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        acc = data.get("account") or {}
        return {"uid": (acc.get("uid") or "?")[:8], "uin": acc.get("uin") or "?"}
    except Exception:
        return None


@register
class WorkBuddyAdapter(AgentAdapter):
    id = "workbuddy"
    label = "WorkBuddy"
    badge = "WB"
    color = "#6366f1"
    sort = 0
    settle_seconds = 3
    needs_restart = False   # 热加载秒切
    has_claim = True
    sign_note = "已通过应用代领每日积分（wbipc 通道，带官方鉴权头）"

    # ---------- 账号快照 ----------
    def state(self) -> dict:
        current = _wb_read_account(WB_AUTH_FILE) if WB_AUTH_FILE.is_file() else None
        accounts = []
        if WB_SNAP_DIR.is_dir():
            for f in sorted(WB_SNAP_DIR.glob("*.info")):
                if not f.is_file():
                    continue
                info = _wb_read_account(f) or {}
                uin, uid = info.get("uin", "?"), info.get("uid", "?")
                # 显示标识：uin（用户号码）> uid，均缺失回退快照名
                display = uin if uin not in ("?", "", None) else (
                    uid if uid not in ("?", "", None) else f.stem)
                accounts.append({
                    "name": f.stem,
                    "display": display,
                    "sub": f"uid {uid} · uin {uin}",
                    "mtime": int(f.stat().st_mtime) if f.exists() else 0,
                    "active": bool(current and uid != "?" and uid == current.get("uid")),
                })
        # 合并 token 直连账号（name 以 "token:" 前缀区分，不参与桌面切换）
        if _wbi is not None:
            try:
                for t in _wbi.list_accounts():
                    sub = f"uid {t['uid'][:8]} · API 直连签到"
                    if t.get("phone"):
                        sub = f"尾号 {t['phone'][-4:]} · " + sub
                    if t.get("refreshLeftDays", 99) < 7:
                        sub += f" · ⚠凭证 {t['refreshLeftDays']} 天后过期"
                    accounts.append({
                        "name": f"token:{t['uid']}",
                        "display": t["nickname"],
                        "sub": sub,
                        "mtime": int(t.get("mtime") or 0),
                        "active": False,
                        "token": True,
                    })
            except Exception:  # noqa: BLE001
                pass
        cur_label = None
        if current:
            hit = next((a["display"] for a in accounts if a["active"]), None)
            cur_label = hit or f"未保存账号 (uid {current['uid']})"
        return {"current": current, "currentLabel": cur_label, "accounts": accounts,
                "scriptFound": WB_SCRIPT is not None}

    def save(self, name: str, force: bool = False) -> dict:
        return proc.run_shell(WB_SCRIPT, "未找到 wb-switch.sh", "save", name, force=force)

    def switch(self, name: str) -> dict:
        return proc.run_shell(WB_SCRIPT, "未找到 wb-switch.sh", "switch", name)

    def delete(self, name: str) -> dict:
        if name.startswith("token:"):
            if _wbi is None:
                return {"ok": False, "output": "wb_import 模块不可用"}
            r = _wbi.remove_account(name.split(":", 1)[1])
            logs.append_log(self.id, r.get("ok", False),
                            f"[删除] {r.get('output', '')}", trigger="delete")
            return r
        f = WB_SNAP_DIR / f"{name}.info"
        if not f.is_file():
            return {"ok": False, "output": f"无此快照: {name}"}
        try:
            f.unlink()
            return {"ok": True, "output": f"已删除快照「{name}」"}
        except OSError as e:
            return {"ok": False, "output": f"删除失败: {e}"}

    # ---------- 签到领取 ----------
    def claim(self, account_display: str, since_ts: int):
        """调 wb_checkin.py claim：应用进程用当前会话向官方接口领取每日积分。"""
        try:
            r = proc.run_python(WB_DIR / "wb_checkin.py", "claim", timeout=150)
        except Exception as e:  # noqa: BLE001
            return False, f"领取异常: {e}"
        if r.get("ok") and r.get("claimed"):
            return True, f"签到成功，+{r.get('credit')} 积分（连续 {r.get('streak_days')} 天）"
        if r.get("ok") and r.get("already"):
            return True, "今日已签过，无需重复领取"
        return False, f"签到失败: {r.get('msg') or r.get('output') or r.get('error') or '未知错误'}"

    def direct_checkin(self, name: str) -> dict | None:
        if not name.startswith("token:") or _wbi is None:
            return None
        res = _wbi.checkin_accounts(name.split(":", 1)[1])
        r0 = res[0] if res else {"ok": False, "msg": "账号不存在"}
        return {"account": name, "ok": bool(r0.get("ok")), "msg": r0.get("msg", "")}

    # ---------- 扩展能力 ----------
    def features(self) -> list[Feature]:
        return [self._growth_feature(), self._import_feature()]

    # 成长任务：wbipc 轻量通道 + 桌面端 CDP 通道（见 Workbuddy/wb_growth.py）
    _growth_status_cache = None

    def _growth_status(self) -> dict:
        if _wg is None:
            return {"ok": False, "error": "wb_growth 模块不可用（检查 Workbuddy/wb_growth.py）"}
        try:
            st = _wg.get_status()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        if st.get("ok"):
            st["badge"] = f"{st.get('completed', 0)}/{st.get('total', 0)}"
        return st

    def growth_status_cached(self, force: bool = False) -> dict:
        """任务概览（供前端按钮数字与弹窗列表）；默认带 60s 缓存，force=1 绕过。"""
        now = time.time()
        if (not force and self._growth_status_cache
                and now - self._growth_status_cache[0] < 60):
            return self._growth_status_cache[1]
        st = self._growth_status()
        self._growth_status_cache = (now, st)
        return st

    def _growth_feature(self) -> Feature:
        def run(payload: dict) -> dict:
            if _wg is None:
                return {"ok": False, "output": "wb_growth 模块不可用"}
            codes = [c for c in (payload or {}).get("tasks", [])
                     if isinstance(c, str) and c in _wg.TASK_META]
            if not codes:
                return {"ok": False, "output": "未选择可执行的任务"}

            def target(progress):
                # CDP 任务需要调试端口。开端口会重启应用,而重启会让应用回退到自身
                # 持久化登录(丢失热加载的账号)——先记住当前账号,重启后热加载回来
                if any(c in _wg.CDP_ACTIONS for c in codes) and not _wg.cdp_available():
                    active = next((a["name"] for a in self.state().get("accounts", [])
                                   if a.get("active") and not a.get("token")), None)
                    _wg.ensure_cdp(progress)   # 必要时重启应用(期间勿用该应用)
                    if active:
                        progress(f"重启完成,重新热加载账号 {active} …")
                        sw = self.switch(active)
                        progress(f"账号热加载: {sw.get('output', '')}")
                        time.sleep(3)   # 等应用 fs.watch 热加载生效
                    else:
                        progress("注意:当前账号未保存为快照,重启后应用回退到自身登录,请手动切换回目标账号")
                return _wg.run_tasks(codes, progress=progress)

            def on_done(res):
                logs.append_log(self.id, bool(res.get("ok")),
                                f"[成长任务] {res.get('output', '')}", trigger="growth")
                self._growth_status_cache = None  # 清缓存，让前端立即看到新进度

            return jobs.start(f"{self.id}:growth", target, on_done=on_done)

        return Feature(
            id="growth", label="成长任务", kind="job",
            description="<p>查看<b>成长中心</b>任务进度，勾选后后台自动完成获得积分/能量。</p>"
                        "<p class='g-note'>轻量任务（接受/领奖/兑换/抽奖）经应用 wbipc 通道秒级完成；"
                        "<b>驱动桌面端</b>的任务（聊天/专家/灵感等）需要 WorkBuddy 以调试端口运行"
                        "（终端执行 <code>python3 Workbuddy/desktop.py ensure</code>），"
                        "执行期间会真实操作应用界面，请勿同时使用该应用。</p>",
            start_label="后台执行勾选任务",
            button_class="btn-growth",
            renderer="growth-tasks",
            poll_seconds=2.5,
            status=self.growth_status_cached,
            run=run,
        )

    # 凭证 JSON 导入：token 直连账号池
    def _import_feature(self) -> Feature:
        def import_data(text: str) -> dict:
            if _wbi is None:
                return {"ok": False, "output": "未找到 Workbuddy/wb_import.py"}
            if not text.strip():
                return {"ok": False, "output": "文件内容为空"}
            if len(text) > 1_000_000:
                return {"ok": False, "output": "文件过大（>1MB），请确认选择的是凭证 JSON 文件"}
            r = _wbi.import_data(text)
            if r.get("imported"):
                logs.append_log(self.id, True, "[导入] " + r["output"] + ": "
                                + ", ".join(i["nickname"] for i in r["imported"]), trigger="import")
            elif r.get("skipped"):
                logs.append_log(self.id, False, "[导入] " + r["output"] + ": "
                                + "; ".join(f"{s['name']}({s['reason']})" for s in r["skipped"]),
                                trigger="import")
            return r

        return Feature(
            id="import", label="导入 JSON", kind="import",
            description="导入外部导出的 WorkBuddy 登录凭证 JSON（含 access/refresh token）。"
                        "导入后账号直接经官方 API 签到，无需切换桌面应用；重复导入同一账号会自动更新凭证。",
            import_data=import_data,
        )
