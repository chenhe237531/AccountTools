"""程序入口：python3 -m app 启动 Web 控制台；打包后的二进制也走这里。

  python3 -m app [--port 8800] [--no-open]

另支持隐藏子命令 --exec-script <script.py> [args…]：PyInstaller 冻结形态下，
适配器以「复跑自身二进制」的方式执行仓库内的 Python 核心脚本（子进程 + stdout
流式进度协议保持不变），见 app/core/proc.py。
"""
from __future__ import annotations

import argparse
import sys


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] == "--exec-script":
        import runpy
        if len(argv) < 2:
            sys.exit("用法: <backend> --exec-script <script.py> [args…]")
        script, rest = argv[1], argv[2:]
        sys.argv = [script, *rest]
        runpy.run_path(script, run_name="__main__")
        return

    from app.core import paths

    ap = argparse.ArgumentParser(description="AccountTools 控制台（Web/桌面同源后端）")
    ap.add_argument("--port", type=int, default=paths.default_port())
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args(argv)

    url = f"http://127.0.0.1:{args.port}"
    no_open = args.no_open or paths.FROZEN  # 桌面形态由 Electron 壳加载页面
    print(f"AccountTools 后端已启动: {url}  (Ctrl+C 退出)"
          + ("  [打包形态]" if paths.FROZEN else ""))
    print(f"  数据目录: {paths.DATA_DIR}")
    if not no_open:
        import webbrowser
        webbrowser.open(url)

    import uvicorn
    from app.core.server import create_app
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
