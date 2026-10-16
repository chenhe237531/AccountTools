#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trae 快速切号器 —— 核心脚本

基于对 Trae 1.107.1（ByteDance iCube 系 Electron 应用）登录模块的静态分析结论：

  * 登录态全部保存在 <profile>/User/globalStorage/storage.json 里的若干键中：
        iCubeAuthInfo://icube.cloudide      -> 用户信息 + accessToken / refreshToken
        iCubeAuthInfo://usertag             -> { userId: 用户地区标识 }
        iCubeAuthInfo://icube-dc:<userId>   -> 设备密钥对（与账号一一对应）
        iCubeServerData://icube.cloudide    -> 套餐 / 权益信息（明文 JSON）
  * 值经过 Trae 自研的 byteCrypto 封装：`tc\\x05\\x10\\x00\\x00` 头 + 32 字节随机种子 + AES-128-CBC。
    密钥由「种子 + 固定常量」经 SHA-512 派生，不依赖钥匙串，因此可离线解密（本脚本已纯 Python 移植）。
  * 主进程 stateService 启动时把 storage.json **一次性读入内存**，没有任何文件监视器，
    所以外部改文件不会热加载 —— 切换必须「先退出应用，改文件，再启动」。

因此切号器设计为：
    每个账号的认证键快照  +  先退出应用再写回  +  重启加载  =  秒级切号

用法：
    python3 trae_switch.py list                    # 列出快照与当前账号
    python3 trae_switch.py save 主号               # 把当前登录账号存为快照
    python3 trae_switch.py switch 小号             # 切换（自动退出 → 写回 → 重启）
    python3 trae_switch.py restart                 # 只重启应用
    python3 trae_switch.py decode                  # 解密并打印当前账号详情

    --profile cn|intl   选择 Profile（默认 cn = Trae CN；intl = Trae 国际版）
    --json              机器可读输出（供图形界面调用）
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------
# Profile 定义
# --------------------------------------------------------------------------
APP_SUPPORT = Path.home() / "Library/Application Support"
SWITCHER_ROOT = Path.home() / ".trae-switcher"
SNAP_ROOT = SWITCHER_ROOT / "snapshots"

PROFILES = {
    "cn": {
        "label": "Trae CN",
        "data_dir": "Trae CN",
        "app_path": "/Applications/Trae CN.app",
        "bundle_id": "cn.trae.app",
        "app_name": "Trae CN",
    },
    "intl": {
        "label": "Trae (国际版)",
        "data_dir": "Trae",
        "app_path": "/Applications/Trae.app",
        "bundle_id": "com.trae.app",
        "app_name": "Trae",
    },
}
DEFAULT_PROFILE = "cn"

AUTH_PREFIX = "iCubeAuthInfo://"
SERVER_DATA_KEY = "iCubeServerData://icube.cloudide"
USERINFO_KEY = "iCubeAuthInfo://icube.cloudide"

# 快照名白名单：中文/字母/数字/下划线/连字符，1~32 位
NAME_RE = re.compile(r"^[\w\u4e00-\u9fa5][\w\u4e00-\u9fa5-]{0,31}$", re.UNICODE)


def is_auth_key(key: str) -> bool:
    """判断某个 storage.json 键是否属于登录态。"""
    return key.startswith(AUTH_PREFIX) or key == SERVER_DATA_KEY


# ==========================================================================
# 一、byteCrypto 纯 Python 移植（仅需解密）
# ==========================================================================
_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)
_INV_SBOX = bytes(256)
_buf = bytearray(256)
for _i, _b in enumerate(_SBOX):
    _buf[_b] = _i
_INV_SBOX = bytes(_buf)
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _expand_key(key: bytes):
    """AES-128 密钥扩展，返回 11 个 16 字节轮密钥。"""
    w = [list(key[4 * i:4 * i + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[x] for x in t]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return [bytes(b for word in w[4 * r:4 * r + 4] for b in word) for r in range(11)]


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _decrypt_block(rk, block: bytes) -> bytes:
    s = list(block)
    for i in range(16):
        s[i] ^= rk[10][i]
    for rnd in range(9, 0, -1):
        # InvShiftRows
        for r in range(1, 4):
            row = [s[r + 4 * c] for c in range(4)]
            row = row[-r:] + row[:-r]
            for c in range(4):
                s[r + 4 * c] = row[c]
        # InvSubBytes
        for i in range(16):
            s[i] = _INV_SBOX[s[i]]
        # AddRoundKey
        for i in range(16):
            s[i] ^= rk[rnd][i]
        # InvMixColumns
        for c in range(4):
            a0, a1, a2, a3 = s[4 * c:4 * c + 4]
            s[4 * c + 0] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
            s[4 * c + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
            s[4 * c + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
            s[4 * c + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)
    # 最后一轮
    for r in range(1, 4):
        row = [s[r + 4 * c] for c in range(4)]
        row = row[-r:] + row[:-r]
        for c in range(4):
            s[r + 4 * c] = row[c]
    for i in range(16):
        s[i] = _INV_SBOX[s[i]]
    for i in range(16):
        s[i] ^= rk[0][i]
    return bytes(s)


def aes128_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC 解密 + PKCS#7 去填充。"""
    if not data or len(data) % 16:
        return b""
    rk = _expand_key(key)
    prev = iv
    out = bytearray()
    for off in range(0, len(data), 16):
        blk = data[off:off + 16]
        dec = _decrypt_block(rk, blk)
        out += bytes(x ^ y for x, y in zip(dec, prev))
        prev = blk
    if out and 1 <= out[-1] <= 16:
        out = out[:-out[-1]]
    return bytes(out)


# byteCrypto 常量
_BC_CONST64 = bytes(
    a ^ b
    for a, b in zip(
        bytes((82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
               124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
               84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
               8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37)),
        bytes((31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
               96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
               160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
               23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125)),
    )
)
_BC_MAGIC = bytes((116, 99, 5, 16, 0, 0))  # 't' 'c' 0x05 0x10 0x00 0x00
_BC_SEED_LEN = 32
_BC_HEAD_BYTES = 64  # 明文前缀 = SHA-512(真实负载)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _derive_key_iv(seed: bytes):
    buf = bytearray(128)
    buf[0:64] = _sha512(seed)
    buf[64:128] = _BC_CONST64
    buf[0:64] = _sha512(bytes(buf))
    return bytes(buf[0:16]), bytes(buf[16:32])


def icube_decode(value: str) -> str:
    """解密 storage.json 中的 iCubeAuthInfo 值（base64 字符串）→ 明文。"""
    raw = base64.b64decode(value)
    if raw[:6] != _BC_MAGIC:
        raise ValueError(f"非 byteCrypto 格式，头部={raw[:6].hex()}")
    seed = raw[6:6 + _BC_SEED_LEN]
    key, iv = _derive_key_iv(seed)
    plain = aes128_cbc_decrypt(key, iv, raw[6 + _BC_SEED_LEN:])
    if len(plain) < _BC_HEAD_BYTES:
        raise ValueError("明文过短")
    if _sha512(plain[_BC_HEAD_BYTES:]) != plain[:_BC_HEAD_BYTES]:
        raise ValueError("完整性校验失败（SHA-512 前缀不匹配）")
    return plain[_BC_HEAD_BYTES:].decode("utf-8")


# ==========================================================================
# 二、磁盘读写
# ==========================================================================
def profile_dir(profile: str) -> Path:
    return APP_SUPPORT / PROFILES[profile]["data_dir"]


def storage_json_path(profile: str) -> Path:
    return profile_dir(profile) / "User" / "globalStorage" / "storage.json"


def snap_dir(profile: str) -> Path:
    return SNAP_ROOT / profile


def _read_storage(profile: str) -> dict:
    p = storage_json_path(profile)
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_auth_keys(profile: str) -> dict:
    """读取 storage.json 中的全部登录态键。"""
    try:
        data = _read_storage(profile)
    except Exception:
        return {}
    return {k: v for k, v in data.items() if is_auth_key(k)}


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        try:
            os.replace(tmp, path)
        except OSError:
            # 某些环境（文件策略代理/杀毒软件）会拒绝"rename 覆盖已有文件"。
            # 退化为直接写入：调用方约定仅在应用退出后写 storage.json，风险可控。
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(path, mode)
    finally:
        # 兜底：无论成功失败都不遗留临时文件（成功路径下 replace 后本就不存在）
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _sweep_stale_tmp(directory: Path, max_age: int = 60) -> None:
    """清扫可能因异常退出遗留的临时文件（超过 max_age 秒）。"""
    try:
        now = time.time()
        for f in directory.glob(".*.tmp.*"):
            try:
                if now - f.stat().st_mtime > max_age:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def extract_info(keys: dict) -> dict:
    """从登录态键中解密出可读的账号信息。"""
    info = {
        "userId": "", "username": "", "email": "", "phone": "", "region": "",
        "plan": "", "host": "", "expiredAt": "", "refreshExpiredAt": "",
        "tokenReleaseAt": "", "accountScope": "", "decoded": False,
    }
    raw = keys.get(USERINFO_KEY)
    if raw:
        try:
            obj = json.loads(icube_decode(raw))
            info["decoded"] = True
            info["userId"] = str(obj.get("userId") or "")
            info["host"] = obj.get("host") or ""
            info["expiredAt"] = obj.get("expiredAt") or ""
            info["refreshExpiredAt"] = obj.get("refreshExpiredAt") or ""
            info["tokenReleaseAt"] = obj.get("tokenReleaseAt") or ""
            acc = obj.get("account") or {}
            info["username"] = acc.get("username") or ""
            info["email"] = acc.get("email") or ""
            info["phone"] = acc.get("nonPlainTextMobile") or ""
            info["accountScope"] = acc.get("loginScope") or acc.get("scope") or ""
            reg = obj.get("userRegion") or {}
            info["region"] = reg.get("region") or reg.get("_aiRegion") or ""
        except Exception:
            pass
    # 套餐：iCubeServerData 是明文 JSON
    sd = keys.get(SERVER_DATA_KEY)
    if sd:
        try:
            j = json.loads(sd) if isinstance(sd, str) else sd
            ent = j.get("entitlementInfo") or {}
            info["plan"] = ent.get("identityStr") or ""
        except Exception:
            pass
    # 兜底：从设备密钥键名里取 userId
    if not info["userId"]:
        for k in keys:
            if k.startswith("iCubeAuthInfo://icube-dc:"):
                info["userId"] = k.rsplit(":", 1)[-1]
                break
    return info


def _snap_path(profile: str, name: str) -> Path:
    return snap_dir(profile) / f"{name}.json"


def load_snapshot(profile: str, name: str) -> dict | None:
    p = _snap_path(profile, name)
    if not p.is_file():
        return None
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def delete_snapshot(profile: str, name: str) -> bool:
    """删除一个快照。返回是否实际删除。"""
    if not NAME_RE.match(name):
        raise ValueError("无效的快照名")
    p = _snap_path(profile, name)
    if not p.is_file():
        raise FileNotFoundError(f"无此快照：{name}")
    os.remove(p)
    return True


def list_snapshots(profile: str) -> list:
    out = []
    d = snap_dir(profile)
    if not d.is_dir():
        return out
    _sweep_stale_tmp(d)
    for f in sorted(d.glob("*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                snap = json.load(fh)
        except Exception:
            continue
        out.append({
            "name": f.stem,
            "userId": snap.get("userId", ""),
            "username": snap.get("username", ""),
            "email": snap.get("email", ""),
            "phone": snap.get("phone", ""),
            "region": snap.get("region", ""),
            "plan": snap.get("plan", ""),
            "expiredAt": snap.get("expiredAt", ""),
            "refreshExpiredAt": snap.get("refreshExpiredAt", ""),
            "savedAt": snap.get("savedAt", 0),
            "keyCount": len(snap.get("keys", {})),
        })
    return out


def save_snapshot(profile: str, name: str, keys: dict | None = None, force: bool = False) -> dict:
    """把当前（或指定）登录态存为快照。"""
    if not NAME_RE.match(name):
        raise ValueError("无效的快照名：仅允许中文/字母/数字/下划线/连字符，1~32 位")
    keys = keys if keys is not None else read_auth_keys(profile)
    if not keys:
        raise RuntimeError("未找到登录态：请先在 Trae 中登录一次")
    path = _snap_path(profile, name)
    if path.exists() and not force:
        raise FileExistsError(f"快照已存在：{path.name}（加 --force 覆盖）")
    _sweep_stale_tmp(snap_dir(profile))
    info = extract_info(keys)
    snap = {
        "profile": profile,
        "name": name,
        "savedAt": int(time.time()),
        "userId": info["userId"],
        "username": info["username"],
        "email": info["email"],
        "phone": info["phone"],
        "region": info["region"],
        "plan": info["plan"],
        "expiredAt": info["expiredAt"],
        "refreshExpiredAt": info["refreshExpiredAt"],
        "host": info["host"],
        "keys": keys,
    }
    snap_dir(profile).mkdir(parents=True, exist_ok=True)
    for d in (SWITCHER_ROOT, SNAP_ROOT, snap_dir(profile)):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    _atomic_write_text(path, json.dumps(snap, ensure_ascii=False, indent=2))
    return snap


def merge_snapshot_into_storage(profile: str, snap: dict) -> dict:
    """把快照的登录态键合并回 storage.json（先清除旧账号的同类键）。"""
    path = storage_json_path(profile)
    if not path.is_file():
        raise RuntimeError(f"未找到 {path}：请先启动一次 Trae")
    data = _read_storage(profile)
    removed = [k for k in data if is_auth_key(k)]
    for k in removed:
        data.pop(k, None)
    data.update(snap.get("keys", {}))
    # Trae 自己的迁移逻辑也会把它置为 true
    data["iCubeNativeAppStartSetup"] = True
    _sweep_stale_tmp(path.parent)
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=4))
    return {"removed": removed, "added": list(snap.get("keys", {}).keys())}


# ==========================================================================
# 三、应用进程控制
# ==========================================================================
def _main_pids(profile: str) -> list:
    """检测应用主进程。
    注意:pgrep -f 对含空格路径(/Applications/Trae CN.app/...)匹配失效(实测),
    改用 LaunchServices 的 lsappinfo 按 bundle id 取主进程 pid,可靠且无需权限。
    """
    bundle_id = PROFILES[profile]["bundle_id"]
    try:
        out = subprocess.run(["lsappinfo", "info", "-only", "pid", bundle_id],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    m = re.search(r'"pid"\s*=\s*(\d+)', out)
    if not m:
        return []
    pid = int(m.group(1))
    # 确认进程仍存活(lsappinfo 可能有陈旧记录)
    try:
        subprocess.run(["ps", "-p", str(pid)], capture_output=True, timeout=5, check=True)
        return [pid]
    except Exception:
        return []


def app_running(profile: str) -> bool:
    return bool(_main_pids(profile))


def quit_app(profile: str, timeout: int = 75) -> bool:
    """优雅退出应用并等待主进程结束。"""
    if not app_running(profile):
        return True
    bundle_id = PROFILES[profile]["bundle_id"]
    app_name = PROFILES[profile]["app_name"]
    # 实测 Trae 关闭需清理大量辅助进程，优雅退出最长给 75 秒
    for attempt in range(2):
        subprocess.run(["osascript", "-e", f'tell application id "{bundle_id}" to quit'],
                       capture_output=True)
        subprocess.run(["osascript", "-e", f'quit app "{app_name}"'], capture_output=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            if not app_running(profile):
                time.sleep(1.5)  # 再多等一会儿确保 storage.json 已落盘
                return True
            time.sleep(0.5)
    # 兜底：SIGTERM 主进程
    for pid in _main_pids(profile):
        try:
            os.kill(pid, 15)
        except Exception:
            pass
    deadline = time.time() + 30
    while time.time() < deadline:
        if not app_running(profile):
            time.sleep(1.5)
            return True
        time.sleep(0.5)
    return False


def launch_app(profile: str, wait: int = 15) -> bool:
    """启动应用并确认进程已拉起。"""
    for args in (["open", "-b", PROFILES[profile]["bundle_id"]],
                 ["open", "-a", PROFILES[profile]["app_path"]]):
        try:
            subprocess.run(args, capture_output=True)
        except Exception:
            continue
        deadline = time.time() + wait
        while time.time() < deadline:
            if app_running(profile):
                return True
            time.sleep(0.5)
    return app_running(profile)


# ==========================================================================
# 四、高层动作
# ==========================================================================
def auto_save_current(profile: str) -> str | None:
    """切换前自动回存当前登录账号：命中同 userId 快照则刷新，否则按账号新建。"""
    keys = read_auth_keys(profile)
    if not keys:
        return None
    info = extract_info(keys)
    uid = info["userId"]
    if not uid:
        return None
    # 1) 已有同 userId 的快照 -> 刷新
    for sn in list_snapshots(profile):
        if sn["userId"] == uid:
            save_snapshot(profile, sn["name"], keys=keys, force=True)
            return sn["name"]
    # 2) 新建：优先用邮箱前缀，其次用户名，最后 uid 尾号
    base = ""
    if info["email"]:
        base = re.split(r"[@.]", info["email"])[0]
    elif info["username"] and not info["username"].startswith("用户"):
        base = info["username"]
    if not base:
        base = f"账号-{uid[-6:]}"
    base = re.sub(r"[^\w\u4e00-\u9fa5-]", "_", base)[:24] or f"账号-{uid[-6:]}"
    name, i = base, 1
    while _snap_path(profile, name).exists():
        i += 1
        name = f"{base}-{i}"
    save_snapshot(profile, name, keys=keys, force=True)
    return name


def do_switch(profile: str, name: str, relaunch: bool = True) -> dict:
    """完整切换流程：退出应用 → 回存当前 → 写回快照 → 重启。"""
    snap = load_snapshot(profile, name)
    if snap is None:
        raise FileNotFoundError(f"无此快照：{name}")
    result = {"steps": [], "resaved": None, "relaunched": False}

    was_running = app_running(profile)
    result["steps"].append(f"检测到应用{'正在运行' if was_running else '未运行'}")
    if was_running:
        if not quit_app(profile):
            raise RuntimeError("无法退出 Trae，已中止（避免正在运行的应用覆盖改动）")
        result["steps"].append("已退出 Trae")

    saved = auto_save_current(profile)
    if saved:
        result["resaved"] = saved
        result["steps"].append(f"已回存当前账号 → 快照「{saved}」")

    m = merge_snapshot_into_storage(profile, snap)
    result["steps"].append(f"已写入登录态（清除 {len(m['removed'])} 个旧键，写入 {len(m['added'])} 个新键）")

    if relaunch:
        if launch_app(profile):
            result["relaunched"] = True
            result["steps"].append("已重新启动 Trae")
        else:
            result["steps"].append("⚠️ 已发出启动命令但未检测到 Trae 进程，请手动打开应用（登录态已写入）")
    return result


def do_restart(profile: str) -> dict:
    steps = []
    if app_running(profile):
        if not quit_app(profile):
            raise RuntimeError("无法退出 Trae")
        steps.append("已退出 Trae")
    if launch_app(profile):
        steps.append("已重新启动 Trae")
    else:
        steps.append("⚠️ 已发出启动命令但未检测到 Trae 进程，请手动打开应用")
    return {"steps": steps}


def build_state(profile: str) -> dict:
    keys = read_auth_keys(profile)
    info = extract_info(keys) if keys else {}
    snaps = list_snapshots(profile)
    for sn in snaps:
        sn["active"] = bool(info.get("userId")) and sn["userId"] == info.get("userId")
    return {
        "profile": profile,
        "profileLabel": PROFILES[profile]["label"],
        "current": info if keys else None,
        "running": app_running(profile),
        "storagePath": str(storage_json_path(profile)),
        "snapDir": str(snap_dir(profile)),
        "snapshots": snaps,
    }


# ==========================================================================
# 五、CLI
# ==========================================================================
def _fmt_time(ts) -> str:
    if not ts:
        return "—"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _days_left(iso: str):
    if not iso:
        return None
    try:
        t = iso.replace("Z", "+00:00")
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(t)
        return (dt - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        return None


def _print_state(profile: str) -> None:
    st = build_state(profile)
    cur = st["current"]
    print(f"Profile : {st['profileLabel']}  ({'运行中' if st['running'] else '未运行'})")
    print(f"会话文件 : {st['storagePath']}")
    print(f"快照目录 : {st['snapDir']}")
    if cur:
        d = _days_left(cur.get("expiredAt", ""))
        dtxt = f"{d:.1f} 天" if d is not None else "—"
        print(f"当前账号 : {cur.get('username') or '(未解析)'}  uid={cur.get('userId')}  "
              f"区域={cur.get('region') or '—'}  套餐={cur.get('plan') or '—'}")
        contact = cur.get("email") or cur.get("phone") or "—"
        print(f"           联系方式={contact}  accessToken 剩余 {dtxt}")
    else:
        print("当前账号 : 未登录")
    print(f"快照数量 : {len(st['snapshots'])}")
    for sn in st["snapshots"]:
        d = _days_left(sn.get("refreshExpiredAt", ""))
        dtxt = f"rt剩余{d:.0f}天" if d is not None else "rt —"
        print(f"  - {sn['name']:<14} {sn['username'] or '(未解析)':<16} uid={sn['userId']:<20} "
              f"{sn['plan'] or '—':<6} {_fmt_time(sn['savedAt'])}  {dtxt}"
              + ("  [当前]" if sn.get("active") else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Trae 快速切号器")
    ap.add_argument("--profile", choices=sorted(PROFILES), default=DEFAULT_PROFILE)
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = ap.add_subparsers(dest="cmd")

    p_save = sub.add_parser("save", help="把当前登录账号存为快照")
    p_save.add_argument("name")
    p_save.add_argument("--force", action="store_true")

    p_sw = sub.add_parser("switch", help="切换到指定快照")
    p_sw.add_argument("name")
    p_sw.add_argument("--no-relaunch", action="store_true", help="切换后不自动重启应用")

    sub.add_parser("list", help="列出快照与当前账号")
    p_del = sub.add_parser("delete", help="删除一个快照")
    p_del.add_argument("name")
    sub.add_parser("decode", help="解密并打印当前账号详情")
    sub.add_parser("state", help="输出机器可读状态")
    sub.add_parser("restart", help="重启应用")

    args = ap.parse_args(argv)
    prof = args.profile
    cmd = args.cmd or "list"

    try:
        if cmd == "save":
            snap = save_snapshot(prof, args.name, force=args.force)
            msg = f"已保存快照「{args.name}」：{snap['username'] or '(未解析)'} uid={snap['userId']} 区域={snap['region']}"
            print(json.dumps({"ok": True, "output": msg}, ensure_ascii=False) if args.json else msg)
        elif cmd == "switch":
            res = do_switch(prof, args.name, relaunch=not args.no_relaunch)
            out = "\n".join(res["steps"])
            msg = f"已切换到「{args.name}」\n" + out
            print(json.dumps({"ok": True, "output": msg, **res}, ensure_ascii=False) if args.json else msg)
        elif cmd == "restart":
            res = do_restart(prof)
            out = "\n".join(res["steps"])
            print(json.dumps({"ok": True, "output": out}, ensure_ascii=False) if args.json else out)
        elif cmd == "delete":
            delete_snapshot(prof, args.name)
            msg = f"已删除快照「{args.name}」"
            print(json.dumps({"ok": True, "output": msg}, ensure_ascii=False) if args.json else msg)
        elif cmd == "state":
            print(json.dumps(build_state(prof), ensure_ascii=False, indent=2))
        elif cmd == "decode":
            keys = read_auth_keys(prof)
            if not keys:
                print("未找到登录态")
                return 1
            info = extract_info(keys)
            print(json.dumps(info, ensure_ascii=False, indent=2) if args.json else
                  f"userId={info['userId']} username={info['username']} email={info['email']} "
                  f"region={info['region']} plan={info['plan']} host={info['host']}\n"
                  f"accessToken 过期={info['expiredAt']}  refreshToken 过期={info['refreshExpiredAt']}")
        else:
            if args.json:
                print(json.dumps(build_state(prof), ensure_ascii=False))
            else:
                _print_state(prof)
    except FileExistsError as e:
        out = str(e)
        print(json.dumps({"ok": False, "output": out}, ensure_ascii=False) if args.json else f"[!] {out}")
        return 1
    except FileNotFoundError as e:
        out = str(e)
        print(json.dumps({"ok": False, "output": out}, ensure_ascii=False) if args.json else f"[!] {out}")
        return 1
    except Exception as e:  # noqa: BLE001
        out = f"{type(e).__name__}: {e}"
        print(json.dumps({"ok": False, "output": out}, ensure_ascii=False) if args.json else f"[!] {out}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
