"""路径与环境：开发（源码树）与冻结（PyInstaller 桌面端）两种形态统一解析。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))  # PyInstaller 打包后的桌面后端

if FROZEN:
    # 打包产物布局：onefile 解包目录内含 app/（代码与静态页）与 legacy/（各平台目录原样副本）
    _BUNDLE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    ROOT = _BUNDLE / "legacy"        # Workbuddy/ Trae/ Qoder/ Cursor/ 核心脚本
    STATIC_DIR = _BUNDLE / "app" / "static"
    # 桌面端数据不写入安装目录（mac .app 只读、win 在 Program Files），固定放用户主目录
    DATA_DIR = Path(os.environ.get("ACCOUNTTOOLS_DATA", Path.home() / ".accounttools"))
else:
    ROOT = Path(__file__).resolve().parents[2]   # 仓库根 AccountTools/
    STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
    DATA_DIR = Path(os.environ.get("ACCOUNTTOOLS_DATA", Path(__file__).resolve().parents[1] / "data"))

CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "logs.jsonl"
HOME = Path.home()


def _load_dotenv() -> None:
    """极简 .env 加载（标准库实现）。仅放本地偏好，凭据一律不入库、不经 .env 传递。"""
    cands = [ROOT / ".env", DATA_DIR / ".env"] if FROZEN else [ROOT / ".env"]
    for cand in cands:
        if not cand.is_file():
            continue
        try:
            for raw in cand.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception:
            pass


def default_port() -> int:
    for key in ("ACCOUNTTOOLS_PORT", "SIGNCENTER_PORT"):  # 兼容旧变量名
        if os.environ.get(key):
            try:
                return int(os.environ[key])
            except ValueError:
                pass
    return 8800


_load_dotenv()
