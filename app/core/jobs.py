"""通用后台任务管理器：长任务（成长任务/设备重置等）线程执行 + 进度流查询。

原先 SignCenter 里 growth / trae-reset 两套手写 job 状态机的公共抽象；
任何适配器的 Feature(kind="job") 都经由这里执行。
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from app.core import locks


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> dict:
        with self._lock:
            job = self._jobs.get(key)
            return dict(job) if job else {"running": False, "steps": [], "result": None,
                                          "startedAt": "", "finishedAt": ""}

    def is_running(self, key: str) -> bool:
        with self._lock:
            return bool(self._jobs.get(key, {}).get("running"))

    def start(self, key: str, target: Callable[[Callable[[str], None]], dict],
              *, use_checkin_lock: bool = False,
              on_done: Callable[[dict], None] | None = None) -> dict:
        """启动后台任务。target(progress_cb) -> result dict；进度经 progress_cb(msg) 流式写入。

        use_checkin_lock=True 时与签到/切号流程互斥（非阻塞抢锁，抢不到即拒绝），
        适用于会动登录态/设备标识的任务（如设备重置）。
        """
        with self._lock:
            if self._jobs.get(key, {}).get("running"):
                return {"ok": False, "output": "已有任务在执行中"}
            if use_checkin_lock and not locks.checkin_lock.acquire(blocking=False):
                return {"ok": False, "output": "签到/切号任务正在执行，请稍后再试"}
            self._jobs[key] = {"running": True, "steps": [], "result": None,
                               "startedAt": time.strftime("%H:%M:%S"), "finishedAt": ""}

        def progress(msg: str) -> None:
            with self._lock:
                self._jobs[key]["steps"].append(
                    {"ts": time.strftime("%H:%M:%S"), "msg": str(msg)})

        def worker() -> None:
            try:
                result = target(progress)
                with self._lock:
                    self._jobs[key].update(running=False,
                                           result=result or {"ok": False, "output": "无结果"},
                                           finishedAt=time.strftime("%H:%M:%S"))
                if on_done:
                    try:
                        on_done(result or {})
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._jobs[key].update(
                        running=False,
                        result={"ok": False, "output": f"执行异常 {type(e).__name__}: {e}"},
                        finishedAt=time.strftime("%H:%M:%S"))
                if on_done:
                    try:
                        on_done({"ok": False, "output": f"执行异常: {e}"})
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                if use_checkin_lock:
                    locks.checkin_lock.release()

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "output": "已开始执行（后台运行）"}


jobs = JobManager()
