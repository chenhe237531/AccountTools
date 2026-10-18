"""PyInstaller 打包入口（等价于 python -m app）。

runpy 是 --exec-script 模式的运行依赖：冻结形态下适配器用它复跑 legacy/
目录里的各平台核心脚本（见 app/core/proc.py），必须随包分发。
"""
import runpy  # noqa: F401  (仅作 PyInstaller 依赖分析标记)

from app.__main__ import main

if __name__ == "__main__":
    main()
