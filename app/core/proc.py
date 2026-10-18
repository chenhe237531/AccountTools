"""子进程执行助手：开发形态用当前解释器跑仓库脚本；冻结形态用自身二进制复跑。

各平台核心脚本是「单一逻辑源」，适配器统一经由此处调用，保持 stdout 流式
进度协议（如 trae_reset.py 逐行进度 + 末行 JSON）在两种形态下行为一致。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def python_argv(script: Path, *args: str) -> list[str]:
    """构造「用 Python 运行仓库内脚本」的命令行。"""
    if getattr(sys, "frozen", False):
        # 打包后的后端没有可用的 python 解释器：复跑自身二进制，由入口分派 runpy
        return [sys.executable, "--exec-script", str(script), *args]
    return [sys.executable, str(script), *args]


def run_python(script: Path, *args: str, timeout: int = 240) -> dict:
    """运行脚本并解析末行 JSON 结果（约定：脚本末行输出单行 JSON）。"""
    try:
        proc = subprocess.run(python_argv(script, *args),
                              capture_output=True, text=True, timeout=timeout)
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            return {"ok": proc.returncode == 0,
                    "output": (proc.stdout + proc.stderr).strip() or f"exit={proc.returncode}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"脚本执行超时({timeout}s)"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "output": f"执行失败: {e}"}


def run_shell(script: Path | None, not_found_msg: str, *args, force=False, timeout=120) -> dict:
    """运行 bash 核心脚本（wb-switch.sh / qoder-switch.sh / cursor-switch.sh）。"""
    if script is None:
        return {"ok": False, "output": not_found_msg}
    import os
    env = dict(os.environ)
    if force:
        env["FORCE"] = "1"
    try:
        proc = subprocess.run(["bash", str(script), *args],
                              capture_output=True, text=True, timeout=timeout, env=env)
        return {"ok": proc.returncode == 0,
                "output": (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"脚本执行超时({timeout}s)"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "output": f"执行失败: {e}"}


def popen_python(script: Path, *args: str) -> subprocess.Popen:
    """以行流式方式后台运行脚本（进度逐行读取，末行 JSON 为结果）。"""
    return subprocess.Popen(python_argv(script, *args),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
