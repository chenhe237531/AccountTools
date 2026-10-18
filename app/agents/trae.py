"""Trae CN 适配器：账号快照（Trae/trae_switch.py，单一逻辑源）+ 设备重置。

签到路径：切换账号并重启应用后，trae-autocheckin 扩展在应用内执行
workbench.action.icubeClaimCheckinCredits 自动领取（绕过 mssdk 签名墙），
本适配器轮询 ~/.trae-cn/autocheckin.log 确认结果。
设备重置：Trae/trae_reset.py --cli --json（macOS 版《trae重置工具6.0》移植）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import time

from app.agents import register
from app.agents.base import AgentAdapter, Feature
from app.core import jobs, logs, paths, proc

TRAE_DIR = paths.ROOT / "Trae"
TRAE_MODULE_PATH = TRAE_DIR / "trae_switch.py"
TRAE_RESET_PATH = TRAE_DIR / "trae_reset.py"
TRAE_CHECKIN_LOG = paths.HOME / ".trae-cn" / "autocheckin.log"

_ts = None
if TRAE_MODULE_PATH.is_file():
    try:
        _spec = importlib.util.spec_from_file_location("trae_switch", TRAE_MODULE_PATH)
        _ts = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_ts)
    except Exception as _e:  # noqa: BLE001
        import sys
        print(f"[警告] trae_switch 模块加载失败: {_e}", file=sys.stderr)


def _profile() -> str:
    return os.environ.get("TRAE_PROFILE", "cn")


@register
class TraeAdapter(AgentAdapter):
    id = "trae"
    label = "Trae CN"
    badge = "TR"
    color = "#0ea5e9"
    sort = 1
    settle_seconds = 8
    always_relaunch = True   # 扩展只在应用启动时激活：同号也要重启以触发领取
    needs_restart = True
    has_claim = True

    def state(self) -> dict:
        if _ts is None:
            return {"current": None, "currentLabel": None, "accounts": [],
                    "scriptFound": False}
        try:
            st = _ts.build_state(_profile())
        except Exception as e:  # noqa: BLE001
            return {"current": None, "currentLabel": None, "accounts": [],
                    "scriptFound": False, "error": str(e)}
        accounts = [{
            "name": sn["name"],
            # 显示标识：邮箱 > 手机号 > userId，均缺失回退快照名
            "display": sn.get("email") or sn.get("phone") or sn.get("userId") or sn["name"],
            "sub": f"{sn.get('username') or '(未解析)'} · {sn.get('plan') or '—'} · uid {sn.get('userId', '?')}",
            "mtime": sn.get("savedAt", 0),
            "active": bool(sn.get("active")),
        } for sn in st.get("snapshots", [])]
        cur = st.get("current")
        cur_label = None
        if cur and cur.get("userId"):
            hit = next((a["display"] for a in accounts if a["active"]), None)
            cur_label = hit or f"未保存账号 ({cur.get('email') or cur.get('phone') or cur.get('userId')})"
        return {"current": cur, "currentLabel": cur_label, "accounts": accounts,
                "scriptFound": True, "running": st.get("running"),
                "profileLabel": st.get("profileLabel", "Trae CN")}

    def save(self, name: str, force: bool = False) -> dict:
        return proc.run_python(TRAE_MODULE_PATH, "--profile", _profile(), "--json",
                               "save", name, *(["--force"] if force else []))

    def switch(self, name: str) -> dict:
        return proc.run_python(TRAE_MODULE_PATH, "--profile", _profile(), "--json",
                               "switch", name)

    def delete(self, name: str) -> dict:
        return proc.run_python(TRAE_MODULE_PATH, "--profile", _profile(), "--json",
                               "delete", name)

    # ---------- 签到领取：等应用内扩展写入领取结果日志 ----------
    def claim(self, account_display: str, since_ts: int, max_wait: int = 45):
        deadline = time.time() + max_wait
        # 扩展日志用 new Date().toISOString()（UTC），比较前缀需用同一时区
        since_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(since_ts))
        while time.time() < deadline:
            try:
                lines = TRAE_CHECKIN_LOG.read_text(
                    encoding="utf-8", errors="replace").splitlines()[-30:]
            except Exception:
                return None, None
            for ln in reversed(lines):
                if since_ts and ln[:19] < since_iso:
                    break  # 已翻到本轮重启之前的旧日志
                if "claim executed" in ln:
                    return True, "扩展已执行领取命令（应用内自动领取完成）"
                if "no claim needed" in ln:
                    return True, "今日已签或未开放（扩展确认无需领取）"
                if "claim error" in ln:
                    return False, f"扩展领取报错: {ln.split('claim error')[1][:80]}"
            time.sleep(3)
        return None, None

    # ---------- 扩展能力 ----------
    def features(self) -> list[Feature]:
        if not TRAE_RESET_PATH.is_file():
            return []
        return [self._reset_feature()]

    def _reset_feature(self) -> Feature:
        def run(payload: dict) -> dict:
            def target(progress):
                proc_obj = proc.popen_python(TRAE_RESET_PATH, "--cli", "--json")
                last_json = None
                for line in proc_obj.stdout:
                    line = line.rstrip("\n")
                    if not line.strip():
                        continue
                    # 脚本末行输出的 JSON 结果不进日志，普通行作为进度流式展示
                    if line.startswith("{") and line.endswith("}"):
                        try:
                            last_json = json.loads(line)
                            continue
                        except Exception:
                            pass
                    progress(line)
                try:
                    proc_obj.wait(timeout=600)
                except Exception:  # noqa: BLE001
                    proc_obj.kill()
                    last_json = {"ok": False, "output": "执行超时(600s)，已终止"}
                return last_json or {"ok": proc_obj.returncode == 0,
                                     "output": f"exit={proc_obj.returncode}"}

            def on_done(res):
                logs.append_log(self.id, bool(res.get("ok")),
                                f"[设备重置] {res.get('output', '')}", trigger="trae-reset")

            # 与签到/切号互斥：重置中途切号（或切号中途重置）都会破坏登录态
            return jobs.start(f"{self.id}:reset", target,
                              use_checkin_lock=True, on_done=on_done)

        return Feature(
            id="reset", label="设备重置", kind="job",
            description="<p>把本机 Trae 识别为<b>全新设备</b>（适用于「设备数量已达上限」时）：</p>"
                        "<ul><li>重写 <code>machineid</code> 与 <code>storage.json</code> 三个 telemetry "
                        "设备标识，清除登录凭据键（会掉线），清空全部本地缓存；</li>"
                        "<li>给客户端 <code>main.js</code> 注入 16 位设备ID读取分支（幂等，原始文件自动备份）；</li>"
                        "<li>为每个数据目录写入全新 16 位设备ID。</li></ul>"
                        "<p style='color:var(--yellow)'>执行前请<b>完全退出 Trae</b>（工具也会自动检测并拒绝）。"
                        "重置后重新打开 Trae，直接点个人用户登录即可恢复，请勿清理浏览器缓存。</p>",
            confirm="确定重置本机 Trae 设备标识？\n\n"
                    "· 需先完全退出 Trae（工具会自动检测，运行中会拒绝执行）\n"
                    "· 当前账号会掉线：重置后重新打开 Trae，直接点个人用户登录即可\n"
                    "· machineid / storage.json / 全部缓存会被清除（原文件自动备份）\n"
                    "· 客户端 main.js 会注入16位设备ID分支（原文件自动备份，幂等）\n\n继续？",
            start_label="开始设备重置",
            button_class="btn-reset",
            poll_seconds=2.0,
            run=run,
        )
