"""AgentAdapter 基类与 Feature 声明：所有软件 agent 适配器的契约。

一个「软件 agent」= 桌面上一个可切号的 AI 编程应用（WorkBuddy / Trae / Qoder /
Cursor / 未来更多…）。适配器把该应用的账号快照、切换、签到领取等能力以统一
接口暴露给编排层与前端；具体动作仍委托各平台目录下的核心脚本或模块执行。
"""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

# 快照名白名单：字母/数字/下划线/连字符/中文，1~32 位，禁止路径字符
import re
NAME_RE = re.compile(r"^[\w一-龥][\w一-龥-]{0,31}$", re.UNICODE)


def valid_name(name) -> bool:
    return isinstance(name, str) and bool(NAME_RE.match(name))


@dataclass
class Feature:
    """适配器向 UI 声明的扩展能力。

    kind 三种：
      "job"    后台长任务：run(payload) 启动（内部走 JobManager），job 进度流轮询展示；
               可提供 status() 返回 {ok, badge, ...} 作为按钮角标与弹窗首屏。
      "action" 即时动作：run(payload) 直接执行并返回 {ok, output}。
      "import" 凭证导入：import_data(text) 解析外部导出的 JSON 并入池。

    renderer 非空时前端使用专用渲染器（当前内置 "growth-tasks"），
    其余走通用「描述 + 进度流」或文件导入 UI，无需改前端。
    """
    id: str
    label: str
    kind: str
    description: str = ""                    # 弹窗说明（允许内联 HTML）
    confirm: str = ""                        # 执行前二次确认文案（空 = 不确认）
    start_label: str = ""                    # 启动按钮文字（默认「开始执行」）
    button_class: str = ""                   # 前端按钮附加样式（如 "btn-reset"）
    renderer: str = ""                       # 前端专用渲染器 id
    poll_seconds: float = 2.5                # job 进度轮询间隔
    status: Callable[[], dict] | None = None
    run: Callable[[dict], dict] | None = None
    import_data: Callable[[str], dict] | None = None
    available: Callable[[], tuple[bool, str]] | None = None  # (可用, 不可用原因)

    def available_ok(self) -> tuple[bool, str]:
        if self.available is None:
            return True, ""
        return self.available()


class AgentAdapter(ABC):
    """适配器契约。子类至少实现 state/save/switch/delete 四个账号快照方法。"""

    id: str = ""            # 唯一标识（配置键、API 路径段）
    label: str = ""         # 展示名
    badge: str = ""         # 卡片角标（≤2 字符）
    color: str = "#3b82f6"  # 主题色
    sort: int = 50          # 卡片排序
    supported_os: tuple = ("darwin",)  # sys.platform 取值；当前四款适配器均依赖 macOS 核心脚本

    # ---- 签到编排参数 ----
    settle_seconds: int = 5       # 切换后等待登录态生效的秒数
    always_relaunch: bool = False  # 同号也重启（扩展只在应用启动时激活的场景）
    needs_restart: bool = True     # 切换是否需要退出并重启应用（前端提示用）
    has_claim: bool = False        # 是否有原生领取能力（覆盖 claim 即视为 True）
    sign_note: str = ""            # 无独立领取接口时的签到说明

    # ---- 系统能力 ----
    def available(self) -> tuple[bool, str]:
        """当前系统是否可运行该 agent 的核心脚本。"""
        if sys.platform in self.supported_os:
            return True, ""
        names = {"darwin": "macOS", "win32": "Windows", "linux": "Linux"}
        want = "/".join(names.get(p, p) for p in self.supported_os)
        return False, f"该 agent 的核心脚本仅支持 {want}，当前系统不可用"

    # ---- 账号快照（必需） ----
    @abstractmethod
    def state(self) -> dict:
        """{"current":…, "currentLabel":…, "accounts":[{name,display,sub,mtime,active,token?}],
        "scriptFound": bool, "error"?: str}"""

    @abstractmethod
    def save(self, name: str, force: bool = False) -> dict:
        """把当前登录态保存为快照，返回 {ok, output}。"""

    @abstractmethod
    def switch(self, name: str) -> dict:
        """切换到指定快照，返回 {ok, output}。"""

    @abstractmethod
    def delete(self, name: str) -> dict:
        """删除指定快照（仅本地文件，不动应用内登录态）。"""

    # ---- 签到领取（可选） ----
    def claim(self, account_display: str, since_ts: int):
        """切换后的原生领取动作。返回 (ok, msg)；ok=None 表示异步领取中（如应用内扩展）。"""
        return None, ""

    # ---- 直连账号（可选） ----
    def direct_checkin(self, name: str) -> dict | None:
        """非快照账号（如导入的 token 账号，name 以 "token:" 前缀标记）的 API 直连签到。

        返回 None 表示该账号不是直连账号，走通用「切换→领取」流程。
        """
        return None

    # ---- 扩展能力（可选） ----
    def features(self) -> list[Feature]:
        return []
