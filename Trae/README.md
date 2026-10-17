# Trae 平台工具集

Trae / Trae CN 账号与设备管理脚本（macOS）。

## trae_switch.py — 快速切号器

保存/切换/删除 Trae 登录态快照（详见脚本内说明）。用法：

```bash
python3 trae_switch.py --profile cn save 快照名
python3 trae_switch.py --profile cn switch 快照名
```

## trae_reset.py — 设备标识重置工具（macOS 版）

Windows 版《trae重置工具 6.0》的 macOS 移植版（逻辑经逆向还原并逐项验证）。
适用于切号过多提示「设备数量已达上限」时，让 Trae 把本机识别为全新设备。

一次重置会做四件事：

1. **machineid** 重写为新 UUID（原文件备份为 `*.traereset_bak_时间戳`）；
2. **storage.json** 三个 telemetry 设备标识（machineId/devDeviceId/sqmId）全部换新，
   并清除登录凭据键（`token/auth/cookie/...`，所以运行后会掉线，重新点个人用户登录即可）；
3. **全部缓存目录/文件** 删除（Cache、Code Cache、IndexedDB、Local Storage 等 25 类）；
4. **客户端 main.js** 注入 16 位设备ID读取分支 `AHA_ID_CLEANER_BRIDGE_V1`
   （幂等，原文件备份到 `~/Library/Application Support/Trae设备ID备份/`），
   并为每个数据目录写入全新 16 位设备ID（`aha-device-id.override.json`）。

用法：

```bash
python3 trae_reset.py            # 图形界面（需 tkinter，如 SignCenter/.venv）
python3 trae_reset.py --cli      # 命令行直接重置
python3 trae_reset.py --cli --detect   # 仅检测（只读，不做任何修改）
python3 trae_reset.py --cli --json     # 末行输出 JSON 结果（供 SignCenter 调用）
python3 trae_reset.py --cli --data-dir ~/Library/Application\ Support/Trae\ CN   # 指定数据目录
```

注意事项：

- 执行前**完全退出 Trae**，运行中会自动检测并拒绝；
- 重置后账号掉线属正常：重新打开 Trae，直接点个人用户登录，**别清理浏览器缓存**；
- 若 main.js 注入报 `Operation not permitted`：系统设置 → 隐私与安全性 → 应用管理，
  给终端/Python 勾选权限后重试，或用 `sudo` 运行。

## SignCenter 集成

「多平台自动签到中心」Trae CN 卡片上的 **设备重置** 按钮即调用本脚本
（`POST /api/trae/reset`，后台执行 + 实时日志轮询）。
