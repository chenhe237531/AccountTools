# -*- mode: python ; coding: utf-8 -*-
# AccountTools 桌面端后端打包（onefile，输出 dist/agenttools-backend[.exe]）
#
#   pyinstaller scripts/backend.spec --noconfirm
#
# 产物布局（与 app/core/paths.py 的冻结路径约定一致）：
#   app/static       前端单页与图标
#   legacy/Workbuddy|Trae|Qoder|Cursor   各平台核心脚本（单一逻辑源原样副本）
import os
import pathlib

# spec 以自身所在目录(scripts/)解析相对路径，统一换算成仓库根的绝对路径
ROOT = os.path.dirname(SPECPATH)

# app 包全量模块清单（源码态静态导入图 + 运行期动态加载的 agents 插件）。
# 与 app/agents/__init__.py 的 AGENT_MODULES 保持同步；
# 不用 collect_submodules()：其在 spec 子进程里可能 import 不到源码树而静默返回空。
APP_MODULES = [
    "app", "app.__main__",
    "app.core", "app.core.config", "app.core.jobs", "app.core.locks",
    "app.core.logs", "app.core.orchestrator", "app.core.paths",
    "app.core.proc", "app.core.server",
    "app.agents", "app.agents.base",
    "app.agents.workbuddy", "app.agents.trae", "app.agents.qoder",
    "app.agents.cursor",
]


def _tree(src, prefix, skip_names=("__pycache__", ".DS_Store"), skip_suffixes=(".pyc",)):
    """收集目录下全部文件为 datas 元组（跳过缓存/系统文件）。"""
    out = []
    for p in sorted(pathlib.Path(src).rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if any(part in skip_names for part in rel.parts) or p.suffix in skip_suffixes:
            continue
        out.append((str(p), f"{prefix}/{rel.parent}"))
    return out

# legacy 核心脚本经 --exec-script(runpy) 在运行期执行，其标准库依赖需显式收进包里
RUNTIME_STDLIB = [
    "argparse", "base64", "datetime", "hashlib", "hmac", "json", "os", "re",
    "runpy", "secrets", "shutil", "socket", "stat", "subprocess", "sys",
    "threading", "time", "urllib.error", "urllib.parse", "urllib.request",
    "uuid", "webbrowser",
    # 第三方：desktop.py(ensure_cdp 自动重启开调试端口)经 runpy 运行期导入
    "requests",
    # uvloop getaddrinfo 在事件循环里懒加载 idna 编解码器，缺失会卡死启动
    "encodings.idna",
]

datas = [(os.path.join(ROOT, "app", "static"), "app/static")]
for _name in ("Workbuddy", "Trae", "Qoder", "Cursor"):
    datas += _tree(os.path.join(ROOT, _name), f"legacy/{_name}")

a = Analysis(
    [os.path.join(ROOT, "backend_entry.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=APP_MODULES + RUNTIME_STDLIB,
    excludes=["playwright", "tkinter", "test", "unittest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="agenttools-backend",
    debug=False,
    strip=False,
    upx=False,
    console=True,          # 后端进程，日志由 Electron 壳收集
)
