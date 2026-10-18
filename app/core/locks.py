"""全局互斥锁：签到/切号流程与后台重置类任务互斥，避免交叉操作登录态。"""
from __future__ import annotations

import threading

# 同一时刻只允许一个签到/切号流程（orchestrator 持有；设备重置等任务可选共享）
checkin_lock = threading.Lock()
