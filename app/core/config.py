"""运行配置（data/config.json）：自动签到偏好 + 各 agent 启停与钩子。"""
from __future__ import annotations

import json
import os
import threading

from app.core import paths

_config_lock = threading.Lock()

# 非平台键的默认值；platforms 子表按注册表里的 agent 动态生成（后注册的新 agent 自动获得默认配置）
_BASE_DEFAULTS = {
    "auto_enabled": True,           # 自动签到总开关
    "auto_resign": True,            # 自动补签：错过时间点后在当天补跑
    "auto_time": "05:20",           # 每天自动签到时间 HH:MM
    "restore_after_checkin": True,  # 签到完成后切回原来的账号
    "last_auto_date": "",           # 最近一次自动签到日期（内部状态）
}


def default_config(agent_ids: list[str]) -> dict:
    cfg = json.loads(json.dumps(_BASE_DEFAULTS))
    cfg["platforms"] = {aid: {"enabled": True, "checkin_command": ""} for aid in agent_ids}
    return cfg


def load_config(agent_ids: list[str]) -> dict:
    cfg = default_config(agent_ids)
    try:
        user = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(user, dict):
            for k, v in user.items():
                if k == "platforms" and isinstance(v, dict):
                    for pid, pv in v.items():
                        if pid in cfg["platforms"] and isinstance(pv, dict):
                            cfg["platforms"][pid].update(pv)
                        # 未知平台（已下线的 agent）的配置原样保留，不丢用户数据
                        elif isinstance(pv, dict):
                            cfg["platforms"][pid] = pv
                else:
                    cfg[k] = v
    except Exception:
        # 旧版 SignCenter 数据目录的一次性迁移（仅开发形态、且新目录尚无数据时）
        if not paths.FROZEN and not paths.CONFIG_PATH.exists():
            _migrate_legacy_signcenter(cfg)
    return cfg


def _migrate_legacy_signcenter(cfg: dict) -> None:
    legacy = paths.ROOT / "SignCenter" / "data"
    for name in ("config.json", "logs.jsonl"):
        src = legacy / name
        if not src.is_file():
            continue
        try:
            paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
            dst = paths.DATA_DIR / name
            if not dst.exists():
                dst.write_bytes(src.read_bytes())
        except Exception:
            pass
    # 迁移完成后重读一次用户配置
    try:
        user = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(user, dict):
            for k, v in user.items():
                if k == "platforms" and isinstance(v, dict):
                    for pid, pv in v.items():
                        if pid in cfg["platforms"] and isinstance(pv, dict):
                            cfg["platforms"][pid].update(pv)
                else:
                    cfg[k] = v
    except Exception:
        pass


def save_config(cfg: dict) -> None:
    with _config_lock:
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = paths.CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, paths.CONFIG_PATH)
