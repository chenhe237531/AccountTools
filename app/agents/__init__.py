"""软件 agent 适配器注册表：加载 AGENT_MODULES 里声明的适配器模块并实例化。

新增一个 agent 只需两步：
  1. 在本目录新建一个模块文件，定义 AgentAdapter 子类并用 @register 装饰；
  2. 在下方 AGENT_MODULES 追加模块名。

核心脚本/数据仍放各自平台目录（单一逻辑源），适配器只做桥接。Web 端与
桌面端共享同一注册表——新 agent 无需改动任何 core/ 或前端结构，卡片、
签到编排、配置开关会自动出现（富交互可通过 Feature.renderer 扩展）。

说明：不用 pkgutil 自动扫描是刻意的——PyInstaller 冻结后纯 Python 模块位于
PYZ 归档内，文件系统扫描结果为空；显式清单在源码态与打包态行为一致。
"""
from __future__ import annotations

import importlib
import sys

# 适配器模块清单（按此顺序加载，卡片排序由 adapter.sort 决定）
AGENT_MODULES = ("workbuddy", "trae", "qoder", "cursor")

_REGISTRY: dict[str, object] = {}
_discovered = False


def register(cls):
    """类装饰器：@register 即加入注册表（按 id 索引）。"""
    _ensure_discovered()
    inst = cls()
    _REGISTRY[inst.id] = inst
    return cls


def _ensure_discovered() -> None:
    global _discovered
    if _discovered:
        return
    for name in AGENT_MODULES:
        try:
            importlib.import_module(f"{__name__}.{name}")
        except Exception as e:  # noqa: BLE001
            print(f"[警告] agent 模块 {name} 加载失败: {e}", file=sys.stderr)
    _discovered = True


def all_agents() -> dict:
    """{agent_id: adapter}，按 adapter.sort 升序（同序按 label）。"""
    _ensure_discovered()
    return dict(sorted(_REGISTRY.items(),
                       key=lambda kv: (getattr(kv[1], "sort", 50), kv[1].label)))


def get(agent_id: str):
    _ensure_discovered()
    return _REGISTRY.get(agent_id)
