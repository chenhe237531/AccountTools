#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trae重置工具(macOS 版) - 重置 Trae IDE 本地设备标识

本工具操作本机 Trae 的用户数据（machineid / storage.json / 全部缓存目录），
并给客户端 main.js 注入 16 位设备ID读取分支（幂等，原文件自动备份），
为每个数据目录写入全新 16 位设备ID（对抗视频生成限流）。
不修改 Trae 程序逻辑，不涉及任何网络请求。
适用于：切换账号过多导致「设备数量已达上限」时，让 Trae 把本机识别为新设备。

本文件是 Windows 版《trae重置工具 6.0》的 macOS 移植版（逻辑经逆向还原，
路径与进程检测已适配 darwin；Windows 分支同样保留，双平台可共用一份代码）。

使用：
    python3 trae_reset.py           # 图形界面（需要 tkinter）
    python3 trae_reset.py --cli     # 命令行直接重置
    python3 trae_reset.py --cli --detect   # 仅检测，不做任何修改
    python3 trae_reset.py --cli --json     # 末行输出 JSON 结果（供网页调用）

注意（与 Windows 版一致）：
    1. 先完全退出 Trae 再运行，否则会拒绝执行；
    2. 运行后 Trae 账号会掉线——别清理浏览器缓存，直接点个人用户登录即可回到登录态。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import uuid
from datetime import datetime

try:
    import winreg  # type: ignore
except ImportError:
    winreg = None

# --------------------------------------------------------------------------
# 常量（与 Windows 版 6.0 完全一致）
# --------------------------------------------------------------------------
# 数据目录下要整目录删除的缓存
CACHE_DIRS = (
    'Cache', 'Code Cache', 'GPUCache', 'DawnGraphiteCache', 'DawnWebGPUCache',
    'CachedData', 'CachedExtensionVSIXs', 'CachedProfilesData', 'CachedConfigurations',
    'Service Worker/CacheStorage', 'Service Worker/ScriptCache', 'ShaderCache',
    'Crashpad', 'Local Storage', 'Session Storage', 'Network', 'Partitions',
    'blob_storage', 'WebStorage', 'Shared Dictionary', 'Dictionaries',
    'aha', 'ahanet', 'monitor', 'IndexedDB',
)
# 数据目录下要单独删除的缓存文件
CACHE_FILES = ('DIPS', 'DIPS-wal', 'SharedStorage', 'SharedStorage-wal',
               'Local State', 'Preferences')
# storage.json 顶层键命中任一关键词即视为登录凭据，重置时清除（对应“运行后会掉线”）
AUTH_HINTS = ('token', 'auth', 'cookie', 'session', 'account', 'login', 'userinfo',
              'userid', 'refresh', 'access', 'credential', 'passwd', 'signin',
              'oauth', 'bearer', 'email', 'phone', 'avatar', 'nickname', 'member',
              'icube', 'key')

# ---- main.js 设备ID注入（AHA_ID_CLEANER_BRIDGE_V1，与 Windows 版逐字节一致）----
AHA_MODULE = 'out-build/vs/platform/iCubeDeviceRegister/electron-main/iCubeDeviceRegisterMainService.js'
AHA_PREFIX = ('/* AHA_ID_CLEANER_BRIDGE_V1 */\n'
              'import{app as __ahaCleanerAppV1}from"electron";\n'
              'import{readFileSync as __ahaCleanerReadV1}from"node:fs";\n'
              'import{join as __ahaCleanerJoinV1}from"node:path";\n'
              'function __ahaCleanerIdV1(){try{const v=JSON.parse(__ahaCleanerReadV1('
              '__ahaCleanerJoinV1(__ahaCleanerAppV1.getPath("userData"),'
              '"aha-device-id.override.json"),"utf8"));'
              'return v.schema===1&&v.owner==="aha-id-cleaner-v1"'
              '&&typeof v.deviceId==="string"'
              '&&/^[1-7][0-9]{15}$/.test(v.deviceId)?v.deviceId:""}catch{return""}}\n')
AHA_CONFIG_NAME = 'aha-device-id.override.json'
# main.js 中设备服务的两种已知字段布局：旧版短字段 / 当前具名字段
AHA_LAYOUTS = (
    {   # 旧版短字段
        'name': '旧版短字段',
        'original': 'this.r="",this.r?(this.j=this.r,this.m=this.r,this.n=this.r):(this.j=',
        'patched': 'this.r=__ahaCleanerIdV1(),this.r?(this.j=this.r,this.m=this.r,this.n=this.r):(this.j=',
        'get_all_ids': 'getAllIds(){return{deviceId:this.j,localDeviceId:this.n,remoteDeviceId:this.m}}',
        'init_override': 'this.r&&(this.j=this.r,this.m=this.r,this.n=this.r)',
    },
    {   # 当前具名字段
        'name': '当前具名字段',
        'original': ('this.mockDeviceId="",this.mockDeviceId?'
                     '(this.deviceId=this.mockDeviceId,this.remoteDeviceId=this.mockDeviceId,'
                     'this.localDeviceId=this.mockDeviceId):(this.deviceId='),
        'patched': ('this.mockDeviceId=__ahaCleanerIdV1(),this.mockDeviceId?'
                    '(this.deviceId=this.mockDeviceId,this.remoteDeviceId=this.mockDeviceId,'
                    'this.localDeviceId=this.mockDeviceId):(this.deviceId='),
        'get_all_ids': ('getAllIds(){return{deviceId:this.deviceId,'
                        'localDeviceId:this.localDeviceId,remoteDeviceId:this.remoteDeviceId}}'),
        'init_override': ('this.mockDeviceId&&(this.deviceId=this.mockDeviceId,'
                          'this.remoteDeviceId=this.mockDeviceId,'
                          'this.localDeviceId=this.mockDeviceId)'),
    },
)

# main.js 原始文件备份根目录（按安装目录哈希区分，不覆盖）
if sys.platform.startswith('win'):
    AHA_BACKUP_ROOT = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Trae设备ID备份')
elif sys.platform == 'darwin':
    AHA_BACKUP_ROOT = os.path.join(os.path.expanduser('~/Library/Application Support'),
                                   'Trae设备ID备份')
else:
    AHA_BACKUP_ROOT = os.path.join(os.path.expanduser('~/.local/share'), 'Trae设备ID备份')

_aha_patch_lock = threading.Lock()

# 客户端发现（按平台）
if sys.platform == 'darwin':
    CLIENT_APPS = ('Trae CN.app', 'TRAE SOLO CN.app', 'Trae.app')   # .app 名
    CLIENT_DIRS = ('/Applications', os.path.expanduser('~/Applications'))
else:
    CLIENT_PROCS = ('Trae CN.exe', 'TRAE SOLO CN.exe', 'Trae.exe')   # type: ignore[assignment]
    CLIENT_FOLDERS = ('Trae CN', 'TRAE SOLO CN', 'Trae')             # type: ignore[assignment]

# 允许手动覆盖（CLI --app-path / --data-dir，供网页或特殊安装位置使用）
_override_app_path: str | None = None
_override_data_dir: str | None = None


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def gen_uuid() -> str:
    return str(uuid.uuid4())


def gen_machine_id() -> str:
    return secrets.token_hex(32)


def gen_guid() -> str:
    return '{' + str(uuid.uuid4()).upper() + '}'


def gen_aha_device_id() -> str:
    """生成全新 16 位设备ID：首位 1-7 + 15 位数字（与 UID 同格式，
    符合客户端校验 /^[1-7][0-9]{15}$/，如 3717914901648939）"""
    return str(1000000000000000 + secrets.randbelow(7000000000000000))


# --------------------------------------------------------------------------
# 路径发现
# --------------------------------------------------------------------------
def find_trae_dirs() -> list:
    """返回存在的 Trae / TRAE SOLO CN 用户数据目录列表。"""
    found = []
    home = os.path.expanduser('~')
    names = ['Trae', 'Trae CN', 'TraeCN', 'TRAE SOLO CN']
    if _override_data_dir:
        # 手动指定时该路径即数据目录本身
        return [_override_data_dir] if os.path.isdir(_override_data_dir) else []
    if sys.platform.startswith('win'):
        bases = [os.environ.get('APPDATA', ''), os.environ.get('LOCALAPPDATA', '')]
    elif sys.platform == 'darwin':
        bases = [os.path.join(home, 'Library', 'Application Support')]
    else:
        bases = [os.path.join(home, '.config')]
    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        for name in names:
            p = os.path.join(base, name)
            if os.path.isdir(p):
                found.append(p)
    return found


def find_storage_json(trae_dir: str):
    p = os.path.join(trae_dir, 'User', 'globalStorage', 'storage.json')
    return p if os.path.isfile(p) else None


def find_machineid(trae_dir: str):
    p = os.path.join(trae_dir, 'machineid')
    return p if os.path.isfile(p) else None


def is_trae_running() -> bool:
    if sys.platform.startswith('win'):
        try:
            r = subprocess.run(['tasklist'], capture_output=True, text=True)
            if r.returncode == 0:
                low = r.stdout.lower()
                return any(x in low for x in ('trae.exe', 'trae cn', 'trae solo'))
        except Exception:
            pass
        try:
            r = subprocess.run(
                ('powershell', '-NoProfile', '-Command',
                 "(Get-Process -Name 'trae*' -ErrorAction SilentlyContinue | Measure-Object).Count"),
                capture_output=True, text=True, timeout=20)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                return int(r.stdout.strip()) > 0
        except Exception:
            return False
        return False
    try:
        r = subprocess.run(['pgrep', '-i', '-l', 'trae'],
                           capture_output=True, text=True)
        out = r.stdout or ''
        # 过滤本工具自身（如 “trae_reset”），避免自匹配误判
        lines = [ln for ln in out.splitlines()
                 if ln.strip() and 'reset' not in ln.lower()]
        return bool(lines)
    except Exception:
        return False


# --------------------------------------------------------------------------
# 缓存清理
# --------------------------------------------------------------------------
def _on_rm_error(func, path, exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def clear_cache(trae_dir: str, log):
    """删除 Trae 数据目录下的所有缓存目录与缓存文件，返回 (成功数, 失败数, 释放字节)。"""
    ok_n = fail_n = freed = 0
    for d in CACHE_DIRS:
        p = os.path.join(trae_dir, d)
        rel = d.replace('/', os.sep)
        if os.path.isdir(p):
            try:
                for root, dirs, files in os.walk(p):
                    for f in files:
                        try:
                            freed += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
                shutil.rmtree(p, onerror=_on_rm_error)
                if os.path.isdir(p):
                    fail_n += 1
                    log(f'    - 缓存 {rel}: 删除不完整（可能有文件被占用）')
                else:
                    ok_n += 1
                    log(f'    - 缓存 {rel}: 已删除')
            except Exception as e:
                fail_n += 1
                log(f'    - 缓存 {rel}: 删除失败 {e}')
    for name in CACHE_FILES:
        f = os.path.join(trae_dir, name)
        if os.path.isfile(f):
            try:
                freed += os.path.getsize(f)
                os.remove(f)
                ok_n += 1
                log(f'    - 缓存文件 {name}: 已删除')
            except Exception as e:
                fail_n += 1
                log(f'    - 缓存文件 {name}: 删除失败 {e}')
    return ok_n, fail_n, freed


# --------------------------------------------------------------------------
# machineid / storage.json 重置
# --------------------------------------------------------------------------
def _is_auth_key(key: str) -> bool:
    k = key.lower()
    return any(h in k for h in AUTH_HINTS)


def _backup(path: str) -> str:
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    bak = path + '.traereset_bak_' + ts
    shutil.copy2(path, bak)
    return bak


def reset_machineid(path: str):
    old = ''
    if os.path.isfile(path):
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            old = f.read().strip()
    new = gen_uuid()
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new)
    return old, new


def reset_storage(path: str):
    with open(path, 'r', encoding='utf-8-sig', errors='ignore') as f:
        try:
            data = json.load(f)
        except Exception as e:
            return {'ok': False, 'error': f'storage.json 解析失败: {e}'}
    if not isinstance(data, dict):
        return {'ok': False, 'error': 'storage.json 顶层不是对象，已跳过'}
    # 清除登录凭据键（运行后会掉线，重新点个人用户登录即可）
    removed = [k for k in data.keys() if _is_auth_key(k)]
    for k in removed:
        data.pop(k, None)
    data.pop('has_device_id_updated_to_aha', None)
    old_ids = {k: data.get(k) for k in
               ('telemetry.machineId', 'telemetry.devDeviceId', 'telemetry.sqmId')}
    new_ids = {
        'telemetry.machineId': gen_machine_id(),
        'telemetry.devDeviceId': gen_uuid(),
        'telemetry.sqmId': gen_guid(),
    }
    data.update(new_ids)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return {'ok': True, 'removed': removed, 'old_ids': old_ids, 'new_ids': new_ids}


def verify(path: str, expected: str) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception:
        return False
    return expected in content


# --------------------------------------------------------------------------
# main.js 设备ID读取分支注入（AHA_ID_CLEANER_BRIDGE_V1）
# --------------------------------------------------------------------------
def aha_main_path(exe: str) -> str:
    if sys.platform == 'darwin':
        # exe 为 .app bundle：…/Trae CN.app → Contents/Resources/app/out/main.js
        return os.path.join(exe, 'Contents', 'Resources', 'app', 'out', 'main.js')
    return os.path.join(os.path.dirname(exe), 'resources', 'app', 'out', 'main.js')


def _aha_locate(code: str, patched: bool):
    """在 main.js 中定位设备服务覆盖分支，返回 (layout, 偏移)；结构不兼容时抛错"""
    mi = code.find(AHA_MODULE)
    if mi < 0 or code.find(AHA_MODULE, mi + len(AHA_MODULE)) >= 0:
        raise RuntimeError('设备服务模块未识别或不唯一')
    chosen, end = None, -1
    for lay in AHA_LAYOUTS:
        gi = code.find(lay['get_all_ids'], mi)
        if gi < 0 or gi - mi > 12000:
            continue
        if chosen is not None:
            raise RuntimeError('设备字段版本不唯一')
        chosen, end = lay, gi
    if chosen is None:
        raise RuntimeError('设备服务结构不兼容')
    needle = chosen['patched'] if patched else chosen['original']
    at = code.find(needle, mi)
    if at < 0 or at > end or code.find(needle, at + len(needle)) >= 0:
        raise RuntimeError('覆盖分支未识别或不唯一')
    return chosen, at


def _aha_mode(code: str) -> str:
    if code.startswith(AHA_PREFIX):
        return 'bridge'
    if 'AHA_ID_CLEANER_BRIDGE_V1' in code or '__ahaCleanerIdV1' in code:
        raise RuntimeError('发现不完整的覆盖代码，请先还原客户端')
    return 'original'


def aha_ensure_patched(exe: str):
    """确保客户端 main.js 已注入设备ID读取分支（幂等：已注入直接跳过）。
    原始文件备份到 AHA_BACKUP_ROOT（按安装目录哈希区分，不覆盖）。返回 (ok, msg)"""
    mp = aha_main_path(exe)
    if not os.path.isfile(mp):
        return False, '未找到客户端 main.js'
    with _aha_patch_lock:
        try:
            with open(mp, 'rb') as f:
                code = f.read().decode('utf-8')
        except Exception as e:
            return False, f'main.js 读取失败: {e}'
        try:
            if _aha_mode(code) == 'bridge':
                return True, '已注入'
            lay, at = _aha_locate(code, False)
        except Exception as e:
            return False, str(e)
        patched = (AHA_PREFIX + code[:at] + lay['patched']
                   + code[at + len(lay['original']):])
        # 备份原始文件（同一客户端只备份一次；用完整路径哈希区分不同 .app）
        key = hashlib.sha1(exe.lower().encode('utf-8')).hexdigest()[:16]
        bdir = os.path.join(AHA_BACKUP_ROOT, key)
        os.makedirs(bdir, exist_ok=True)
        bp = os.path.join(bdir, 'main.original.js')
        if not os.path.isfile(bp):
            with open(bp, 'wb') as f:
                f.write(code.encode('utf-8'))
        # 原子写入：先写临时文件再替换，避免写一半损坏客户端
        try:
            tmp = os.path.join(os.path.dirname(mp), '.aha-tmp')
            with open(tmp, 'wb') as f:
                f.write(patched.encode('utf-8'))
            os.replace(tmp, mp)
        except PermissionError as e:
            return False, (
                f'main.js 写入失败: {e}\n'
                '      macOS 提示：系统限制修改 /Applications 下的应用。\n'
                '      解决：系统设置 → 隐私与安全性 → 应用管理，给运行本工具的\n'
                '      终端/Python 勾选权限后重试；或用 sudo 运行本工具。')
        except Exception as e:
            return False, f'main.js 写入失败: {e}'
        return True, '已注入设备ID读取分支'


def aha_write_override(user_data_dir: str, dev_id: str):
    """把全新 16 位设备ID写入数据目录（启动时客户端读取它作为视频请求的设备ID）"""
    os.makedirs(user_data_dir, exist_ok=True)
    doc = {
        'schema': 1,
        'owner': 'aha-id-cleaner-v1',
        'deviceId': dev_id,
        'createdAt': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z'),
    }
    with open(os.path.join(user_data_dir, AHA_CONFIG_NAME), 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))
        f.write('\n')


# --------------------------------------------------------------------------
# 客户端发现
# --------------------------------------------------------------------------
def _default_app_candidates() -> list:
    """macOS：/Applications 与 ~/Applications 下的 Trae 系列 .app"""
    if sys.platform != 'darwin':
        return []
    cands = []
    for d in CLIENT_DIRS:
        for name in CLIENT_APPS:
            p = os.path.join(d, name)
            if os.path.isdir(p):
                cands.append(p)
    return cands


def _reg_find_exe() -> list:
    """Windows：从卸载表找 trae 客户端 exe（保留自原版，双平台共用）"""
    if winreg is None:
        return []
    cands, seen = [], set()

    def _add(path):
        p = (path or '').strip().strip('"')
        if not p or p.lower() in seen:
            return
        seen.add(p.lower())
        cands.append(p)

    roots = (r'Software\Microsoft\Windows\CurrentVersion\Uninstall',
             r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for root in roots:
            try:
                k = winreg.OpenKey(hive, root)
            except OSError:
                continue
            with k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        sk = winreg.OpenKey(k, winreg.EnumKey(k, i))
                    except OSError:
                        continue
                    with sk:
                        try:
                            vals = {n: winreg.QueryValueEx(sk, n)[0]
                                    for n in ('DisplayName', 'DisplayIcon',
                                              'InstallLocation', 'UninstallString')
                                    if winreg.QueryValueEx(sk, n)[0]}
                        except OSError:
                            continue
                    if 'trae' not in str(vals.get('DisplayName', '')).lower():
                        continue
                    for key in ('DisplayIcon', 'InstallLocation', 'UninstallString'):
                        _add(str(vals.get(key, '')).split(',')[0])
    return cands


def find_client_exes() -> list:
    """返回可注入 main.js 的客户端位置（macOS 为 .app 路径，Windows 为 exe）。"""
    if _override_app_path:
        return [_override_app_path] if os.path.exists(_override_app_path) else []
    if sys.platform == 'darwin':
        return _default_app_candidates()
    # Windows：常见目录 + 注册表
    folders = ['Programs', os.environ.get('ProgramFiles', r'C:\Program Files'),
               os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')]
    seen, cands = set(), []

    def _add(p):
        p = (p or '').strip().strip('"')
        if p and p.lower() not in seen:
            seen.add(p.lower())
            cands.append(p)

    for folder in folders:
        for sub in CLIENT_FOLDERS:
            base = os.path.join(os.path.expanduser('~'), folder, sub) \
                if folder == 'Programs' else os.path.join(folder, sub)
            for proc in CLIENT_PROCS:
                _add(os.path.join(base, proc))
    for p in _reg_find_exe():
        _add(p)
    # 只保留真实存在、且旁边确实有客户端目录结构的候选
    out = []
    for p in cands:
        if os.path.isfile(p):
            out.append(p)
        elif os.path.isdir(os.path.dirname(p)) and not out:
            pass
    # 注册表里直接找到的 exe 优先补全
    for p in _reg_find_exe():
        if os.path.isfile(p) and p not in out:
            out.append(p)
    return out


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def run_reset(log) -> bool:
    """执行一次完整重置，log 为 callable(str)。返回是否成功。"""
    dirs = find_trae_dirs()
    if not dirs:
        log('[!] 未找到 Trae 用户数据目录。请确认 Trae 已安装，或手动指定路径。')
        return False
    if is_trae_running():
        log('[!] 检测到 Trae 正在运行，请先完全退出 Trae 后再重置。')
        return False

    overall_ok = True

    # 1) 客户端 main.js 注入
    exes = find_client_exes()
    if exes:
        for exe in exes:
            if not os.path.isfile(aha_main_path(exe)):
                log(f'\n[客户端] {exe}')
                log('    - main.js 注入: 旧版客户端无 main.js，跳过（16位设备ID仍会写入数据目录）')
                continue
            ok, msg = aha_ensure_patched(exe)
            log(f'\n[客户端] {exe}')
            log(f'    - main.js 注入: {msg}  [{"OK" if ok else "FAIL"}]')
            if not ok:
                overall_ok = False
    else:
        log('\n[客户端] 未找到 Trae 客户端安装目录，跳过 main.js 注入（仅重置用户数据）。')

    # 2) 逐个数据目录重置
    for trae_dir in dirs:
        log(f'\n[目录] {trae_dir}')
        machineid_path = find_machineid(trae_dir)
        storage_path = find_storage_json(trae_dir)

        ok_n, fail_n, freed = clear_cache(trae_dir, log)
        if ok_n or fail_n:
            log(f'    - 缓存清理: 成功 {ok_n} 个，失败 {fail_n} 个，'
                f'释放 {freed / 1048576:.1f} MB')
        else:
            log('    - 缓存清理: 未发现缓存目录')
        if fail_n:
            overall_ok = False

        if not machineid_path and not storage_path:
            log('    - 未发现 machineid 或 storage.json，跳过。')
            continue

        if machineid_path:
            try:
                bak = _backup(machineid_path)
                old, new = reset_machineid(machineid_path)
                ok = verify(machineid_path, new)
                overall_ok = overall_ok and ok
                log(f'    - machineid: {old or "(空)"} -> {new}  [{"OK" if ok else "FAIL"}]')
                log(f'      备份: {bak}')
            except Exception as e:
                overall_ok = False
                log(f'    - machineid 写入失败: {e}')

        if storage_path:
            try:
                bak = _backup(storage_path)
                res = reset_storage(storage_path)
                if not res.get('ok'):
                    overall_ok = False
                    log(f'    - storage.json: {res.get("error")}')
                    continue
                new_ids = res['new_ids']
                ok = (verify(storage_path, new_ids['telemetry.machineId'])
                      and verify(storage_path, new_ids['telemetry.devDeviceId'])
                      and verify(storage_path, new_ids['telemetry.sqmId']))
                overall_ok = overall_ok and ok
                removed = res.get('removed') or []
                log(f'    - storage.json 已清除凭据键: {removed if removed else "(无)"}')
                log(f'    - telemetry.machineId : {new_ids["telemetry.machineId"][:8]}...'
                    f' [{"OK" if ok else "FAIL"}]')
                log(f'    - telemetry.devDeviceId: {new_ids["telemetry.devDeviceId"]}')
                log(f'    - telemetry.sqmId     : {new_ids["telemetry.sqmId"]}')
                log(f'      备份: {bak}')
            except Exception as e:
                overall_ok = False
                log(f'    - storage.json 处理失败: {e}')

        try:
            dev_id = gen_aha_device_id()
            aha_write_override(trae_dir, dev_id)
            log(f'    - 设备ID(16位): {dev_id}  [OK]')
        except Exception as e:
            overall_ok = False
            log(f'    - 设备ID(16位) 写入失败: {e}')

    if overall_ok:
        log('\n[完成] 设备标识与16位设备ID已重置，所有缓存已清空。'
            '请重新打开 Trae 并登录新账号。')
    else:
        log('\n[警告] 部分步骤未完成，请查看上方日志。')
    return overall_ok


def detect_text() -> str:
    """只读检测：数据目录 / 客户端 / 运行状态 / 注入状态。"""
    lines = []
    dirs = find_trae_dirs()
    lines.append('==== 环境检测 ====')
    lines.append(f'main.js 注入标记: {AHA_MODULE}')
    if not dirs:
        lines.append('[!] 未找到 Trae 用户数据目录')
    for d in dirs:
        lines.append(f'\n[目录] {d}')
        lines.append(f'    machineid  : {"存在" if find_machineid(d) else "无"}')
        lines.append(f'    storage.json: {"存在" if find_storage_json(d) else "无"}')
        mp = os.path.join(d, AHA_CONFIG_NAME)
        if os.path.isfile(mp):
            try:
                with open(mp, encoding='utf-8') as f:
                    doc = json.load(f)
                lines.append(f'    16位设备ID : {doc.get("deviceId")}')
            except Exception:
                lines.append('    16位设备ID : (读取失败)')
    exes = find_client_exes()
    if not exes:
        lines.append('\n[客户端] 未找到（跳过 main.js 注入）')
    for exe in exes:
        lines.append(f'\n[客户端] {exe}')
        mp = aha_main_path(exe)
        if not os.path.isfile(mp):
            lines.append('    main.js: 不存在（旧版客户端）')
            continue
        try:
            with open(mp, encoding='utf-8', errors='ignore') as f:
                head = f.read(len(AHA_PREFIX) + 64)
            lines.append(f'    main.js: 已注入' if head.startswith(AHA_PREFIX)
                         else '    main.js: 未注入')
        except Exception as e:
            lines.append(f'    main.js: 读取失败 {e}')
    lines.append(f'\n[进程] Trae {"正在运行（重置前需先退出）" if is_trae_running() else "未运行"}')
    return '\n'.join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def cli_main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='trae重置工具(macOS 版) - 重置 Trae 本地设备标识')
    ap.add_argument('--cli', action='store_true', help='命令行模式（无 GUI）')
    ap.add_argument('--detect', action='store_true', help='仅检测，不做任何修改')
    ap.add_argument('--json', action='store_true', help='最后输出一行 JSON 结果')
    ap.add_argument('--app-path', help='手动指定客户端 .app / exe 路径')
    ap.add_argument('--data-dir', help='手动指定用户数据目录')
    args = ap.parse_args(argv)

    global _override_app_path, _override_data_dir
    _override_app_path = args.app_path
    _override_data_dir = args.data_dir

    lines = []

    def log(msg):
        print(msg, flush=True)
        lines.append(msg)

    if args.detect:
        txt = detect_text()
        print(txt, flush=True)
        if args.json:
            print(json.dumps({'ok': True, 'output': '检测完成', 'detect': True},
                             ensure_ascii=False), flush=True)
        return 0

    ok = run_reset(log)
    if args.json:
        summary = next((ln for ln in reversed(lines) if ln.startswith('[完成]')
                        or ln.startswith('[警告]') or ln.startswith('[!]')), '')
        print(json.dumps({'ok': bool(ok), 'output': summary or ('成功' if ok else '失败')},
                         ensure_ascii=False), flush=True)
    return 0 if ok else 1


# --------------------------------------------------------------------------
# GUI（tkinter；customtkinter 可用则用，与原版一致的降级策略）
# --------------------------------------------------------------------------
def build_gui():
    try:
        import customtkinter as ctk
        use_ctks = True
        ctk.set_appearance_mode('system')
        ctk.set_default_color_theme('blue')
        Textbox, Button, Label, Frame = (ctk.CTkTextbox, ctk.CTkButton,
                                         ctk.CTkLabel, ctk.CTkFrame)
    except ImportError:
        import tkinter as tk
        from tkinter import scrolledtext, messagebox
        use_ctks = False
        Textbox = Button = Label = Frame = None

    import tkinter as tk
    from tkinter import messagebox

    class TraeResetApp(tk.Tk if not use_ctks else ctk.CTk):
        def __init__(self):
            super().__init__()
            self.title('trae重置工具 6.0 - mac · Trae IDE 设备标识重置')
            self.geometry('680x560')
            if use_ctks:
                self.grid_columnconfigure(0, weight=1)
                self.grid_rowconfigure(1, weight=1)
            self._build()

        def _build(self):
            if use_ctks:
                head = Label(self, text='Trae IDE · 设备标识一键重置（macOS）',
                             font=('Microsoft YaHei', 16, 'bold'))
                head.grid(row=0, column=0, padx=12, pady=(12, 4), sticky='w')
                self.txt = Textbox(self, wrap='word',
                                   font=('Menlo', 12) if sys.platform == 'darwin'
                                   else ('Consolas', 11))
                self.txt.grid(row=1, column=0, padx=12, pady=8, sticky='nsew')
                bar = Frame(self)
                bar.grid(row=2, column=0, padx=12, pady=(0, 12), sticky='ew')
                bar.grid_columnconfigure((0, 1), weight=1)
                self.reset_btn = Button(bar, text='一键重置', command=self.on_reset)
                self.reset_btn.grid(row=0, column=0, padx=4, sticky='ew')
                self.detect_btn = Button(bar, text='环境检测', command=self.on_detect)
                self.detect_btn.grid(row=0, column=1, padx=4, sticky='ew')
            else:
                head = tk.Label(self, text='Trae IDE · 设备标识一键重置（macOS）',
                                font=('Helvetica', 15, 'bold'))
                head.pack(padx=12, pady=(12, 4), anchor='w')
                self.txt = scrolledtext.ScrolledText(self, wrap='word',
                                                     font=('Menlo', 12))
                self.txt.pack(fill='both', expand=True, padx=12, pady=8)
                bar = tk.Frame(self)
                bar.pack(fill='x', padx=12, pady=(0, 12))
                bar.grid_columnconfigure((0, 1), weight=1)
                self.reset_btn = tk.Button(bar, text='一键重置', command=self.on_reset)
                self.reset_btn.grid(row=0, column=0, padx=4, sticky='ew')
                self.detect_btn = tk.Button(bar, text='环境检测', command=self.on_detect)
                self.detect_btn.grid(row=0, column=1, padx=4, sticky='ew')
            self._append('提示：先完全退出 Trae，再点「一键重置」。')
            self._append('重置后账号会掉线，重新点个人用户登录即可。\n')

        def _append(self, msg):
            self.txt.insert('end', msg + '\n')
            try:
                self.txt.see('end')
            except Exception:
                pass

        def on_detect(self):
            self.txt.delete('1.0', 'end')
            self._append(detect_text())

        def on_reset(self):
            self.reset_btn.config(state='disabled')
            self.txt.delete('1.0', 'end')
            self._append('==== 开始重置 ====')

            def worker():
                ok = run_reset(self._append)
                self.after(0, lambda: self._done(ok))

            threading.Thread(target=worker, daemon=True).start()

        def _done(self, ok):
            self._append('==== 重置结束 ====')
            self._show_done_tip(ok)
            self.reset_btn.config(state='normal')

        def _show_done_tip(self, ok):
            top = tk.Toplevel(self)
            top.withdraw()
            try:
                top.attributes('-topmost', True)
            except Exception:
                pass
            messagebox.showinfo(
                'trae重置工具',
                ('重置完成！\n\n请重新打开 Trae：\n'
                 '· 别清理浏览器缓存\n· 直接点个人用户登录即可' if ok
                 else '部分步骤未完成，请查看窗口日志。'),
                parent=top)
            top.destroy()

    app = TraeResetApp()
    app.mainloop()


def main():
    if len(sys.argv) > 1:
        cli_main(sys.argv[1:])
        return
    try:
        build_gui()
    except Exception as e:  # 无 tkinter/GUI 环境 → 回退 CLI
        print(f'[!] GUI 不可用（{e}），切换到命令行模式。\n')
        cli_main(['--cli'])


if __name__ == '__main__':
    main()
