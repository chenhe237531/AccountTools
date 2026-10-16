# Qoder 快速切号器使用文档

> 适用版本：Qoder App 0.4.3 ~ 0.4.8（mac arm64，Electron 应用，bundle id `com.qoder.app`）
> 脚本位置：`~/.qoder/qoder-switch.sh`（本目录为其备份副本）
> 图形界面：本目录 `index.html` + `server.py`（见「图形界面」章节）

## 一、原理概述

Qoder 的登录凭据整体存放在单个加密文件中，**整文件即一个账号的身份**。应用启动时从该文件恢复登录态（日志可见 `Auth state restoration completed`），登录成功时由主进程整体重写。

与 WorkBuddy 不同的是：**Qoder 没有会话文件热加载**（已实测，见下文），因此切号器的设计为：

```
保存每个账号的凭据快照  +  退出应用 → 原子换文件 → 重启应用  =  约 15 秒切号
```

```
┌──────────────┐  save   ┌─────────────────────┐
│ auth.v1.dat  │ ──────► │ ~/.qoder/account-   │
│ (当前账号 A)  │         │ switcher-snapshots/ │
└──────────────┘         │  ├── 主号/          │
      ▲   ▲              │  └── 小号/          │
 switch │  │ restore      └─────────────────────┘
 (换文件+重启)│
┌──────────────┐
│ auth.v1.dat  │  ◄── 整文件原子交换,不碰加密
│ (目标账号 B)  │
└──────────────┘
```

## 二、登录模块逆向分析（REA）

### 1. 应用与登录架构

- 安装器同时分发两个产品：**Qoder App**（Agent 工作台，`com.qoder.app`，Electron 43.1.1）与 **Qoder IDE**（VS Code 系，`com.qoder.ide`，独立 DMG）。**登录态在 Qoder App**。
- 主进程代码（`out/main/electronMain.js`，17MB）重度混淆（字符串表 + base64 多层编码），静态 grep 不可行，关键结论均通过 **动态行为验证** 获得。

### 2. 核心会话文件

```
~/Library/Application Support/com.qoder.app.stable/auth.v1.dat
```

- **格式**：Electron `safeStorage` v10 信封加密（AES-256-GCM），密钥存于登录钥匙串 `Qoder App Safe Storage`（机器本地，`security dump-keychain` 可见）
- **写入时机**：每次登录成功（主进程日志 `Device login completed ... persistenceResult: "persisted"`）以及令牌刷新
- **不重写**：应用启动恢复登录态时**不会**重写该文件（实测 hash 前后一致）
- 403 字节左右，密文内含访问/刷新令牌（无法直接读取字段，也不需要——整文件搬运即可）

伴生文件 `auth-profile-overlays.v1.dat`（同 v10 加密，登录时随账号一起更新，存账号展示信息），一并纳入快照。

### 3. 登录流程（Device Login）

```
应用内点「登录」 → 打开系统浏览器(qoder.com 授权页)
→ 应用后台轮询(Device login poll) → 授权成功取回令牌
→ 持久化到 auth.v1.dat → 刷新 ~/.qoder/.qoder-app-status.json
```

- 全程不需要 `qoder://` 深链回调（日志 `callbackObserved:false`，轮询即完成）
- API 域名 `openapi.qoder.sh`，请求头 `Authorization: Bearer …` + `Cosy-MachineToken` 等机器标识

### 4. 明文账号状态（切号器的识别来源）

```
~/.qoder/.qoder-app-status.json
```

| 字段 | 含义 |
|---|---|
| `logged_in` | 是否已登录 |
| `name` / `email` | 当前账号昵称 / 邮箱 |
| `avatar_url` | 头像 URL，其中 `/users/<uuid>/` 即账号 UUID |
| `snapshot_at` | 状态刷新时间 |

**每次登录和每次应用启动都会刷新**（实测：重启后 `snapshot_at` 更新为启动时刻）。切号器据此识别当前账号，无需解密凭据。

### 5. 热加载验证（无）与重启恢复（有）

| 实验 | 结果 |
|---|---|
| 应用运行中，用相同内容原子重写 `auth.v1.dat` | 主进程日志**零反应** → 无外部修改监视器 |
| 退出 → 重启 Qoder | 日志出现 `[Auth] Auth state restoration completed`，登录态保留，`auth.v1.dat` hash 不变 |

结论：**切换必须走「退出 → 换文件 → 启动」**；重启本身不会丢登录态。

### 6. 账号数据天然隔离/共存

- 账号作用域格式 `qoder:prod:global:<64位hash>`（`qoder-data.v1.ext.json` 的 `mainFileScope`，应用启动时按当前账号重算）
- 渲染层 Local Storage 按账号 UUID 分键共存（实测两个账号的 `account-achievements:v1:<uuid>` 并存，互不覆盖）
- 聊天/工作区数据在 `main.sqlite`（共享，不随账号切换）——切号器**不动**这些数据文件，由应用自行重算作用域
- `~/.qoder/` 下的 `installation_id`、`machine_id`、`device-id` 为机器级共享，切换无需改动

### 7. CLI 认证链路（qodercli 为何不受切换影响）

qodercli 采用 **external 认证策略**：每次会话由 Electron 主进程写一个临时 `jobToken` payload（`$TMPDIR/qoder-sdk-auth-*/payload.json`，权限 600）传给 CLI，CLI 不独立持有用户令牌。因此换 `auth.v1.dat` 后 CLI 认证自动跟随，无需额外处理。

### 8. 与 WorkBuddy 方案的差异对照

| | WorkBuddy | Qoder |
|---|---|---|
| 会话文件 | 明文 JSON（仅 4 字段加密） | 整文件 safeStorage v10 加密 |
| 账号识别 | 直接读 JSON 的 uid/uin | 读明文伴生文件 `.qoder-app-status.json` |
| 热加载 | 有（fs.watch，1~2 秒生效） | **无**（需退出重启，约 15 秒） |
| 登出标记 | `.logged-out` 标记文件 | 未发现（切号器也不走登出流程） |
| CLI/伴生进程 | daemon 自持凭据 | jobToken 按会话下发，自动跟随 |

## 三、切号器用法

首次准备：把脚本放进 `~/.qoder`（推荐）：

```bash
cp qoder-switch.sh ~/.qoder/ && chmod +x ~/.qoder/qoder-switch.sh
```

每个账号在 Qoder 里正常登录一次，然后保存快照：

```bash
~/.qoder/qoder-switch.sh save 主号     # 登录账号 A 后执行
# 在 Qoder 里退出登录 → 登录账号 B
~/.qoder/qoder-switch.sh save 小号
~/.qoder/qoder-switch.sh switch 主号   # 秒级流程:退出→换文件→重启
```

完整命令列表：

| 命令 | 说明 |
|---|---|
| `save <名字>` | 把当前登录账号凭据存为快照（已存在时需 `FORCE=1` 覆盖） |
| `switch <名字>` | 切换到指定账号（自动回存当前账号 → 退出 → 换文件 → 重启） |
| `list` | 列出所有快照与当前账号（姓名/邮箱/UUID） |
| `current` | 显示当前登录状态与会话文件信息 |
| `restart` | 重启 Qoder（登录态不丢，兜底用） |

### 脚本要点

- **切换前自动回存**：按账号 UUID 匹配既有快照并刷新，不丢当前登录态；当前账号无快照且未 `FORCE=1` 时拒绝切换（防误丢）
- **原子写**：临时文件 + rename（与官方写法一致），避免写一半被读到
- **顺序保证**：先等应用完全退出（最长 15 秒）再换文件，杜绝应用退出时回写凭据造成竞争
- **权限控制**：快照目录 700，文件 600
- **同账号幂等**：切换到当前账号时直接提示跳过（`FORCE=1` 可强制重换文件）

## 四、注意事项

1. **快照即凭据**：每个快照含有效的加密令牌，等同于密码文件，**切勿放入网盘 / 代码仓库**。v10 加密绑定本机钥匙串，快照**不可跨机器使用**（同机 FileVault + 0600 权限是基本保障）。
2. **保持 token 活性**：长期不登录的账号令牌可能失效（具体有效期未实证），定期 `switch` 过去一次再切回来即可续活。
3. **避免在应用内点「退出登录」**：切号全程不走登出流程，直接换文件即可；应用内登出可能使服务端会话失效，影响对应快照。
4. **多快照同名账号**：同一账号可存多个快照，回存时刷新 UUID 匹配到的第一个。
5. **合规提示**：本方案为自己机器上的自有账号设计；若账号属企业管控，频繁换号是否合规以相关规定为准。

## 五、文件清单

| 文件 | 位置 | 说明 |
|---|---|---|
| `qoder-switch.sh` | `~/.qoder/`（本目录有副本） | 切号脚本（核心逻辑） |
| `server.py` | 本目录 | 图形界面本地桥接服务 |
| `index.html` | 本目录 | 图形界面页面 |
| 凭据快照 | `~/.qoder/account-switcher-snapshots/<名字>/` | `auth.v1.dat` + `auth-profile-overlays.v1.dat` + `meta.json`（账号指纹） |
| 会话文件 | `~/Library/Application Support/com.qoder.app.stable/auth.v1.dat` | 当前生效凭据（safeStorage v10 加密） |
| 账号状态 | `~/.qoder/.qoder-app-status.json` | 明文账号指纹（登录/启动时刷新） |
| 加密密钥 | 登录钥匙串 `Qoder App Safe Storage` | safeStorage 主密钥（机器本地） |

## 六、图形界面（本地控制台）

命令行之上提供了一层可视化操作界面，由两部分组成：

| 文件 | 作用 |
|---|---|
| `index.html` | 可视化界面（当前账号状态条、快照卡片、操作日志） |
| `server.py` | 本地桥接服务：托管页面并把按钮操作转发给 `qoder-switch.sh` |

### 启动

```bash
cd Qoder
python3 server.py            # 默认 http://127.0.0.1:8790,启动后自动打开浏览器
python3 server.py --port 9000 --no-open   # 自定义端口 / 不自动打开
```

无任何第三方依赖（Python 3 标准库），`Ctrl+C` 退出。与 WorkBuddy 控制台（8787）可同时运行；本机 8788 已被其他工具占用，故默认 8790。

### 界面功能

- **当前账号状态条**：昵称 / 邮箱 / 账号 UUID（来自 `.qoder-app-status.json`），绿色呼吸灯表示已登录，Qoder 运行状态实时显示
- **横幅提醒**：Qoder 未运行（黄色）、会话文件缺失 / 脚本未找到（红色）
- **快照卡片**：每个账号一张卡片，显示账号昵称 / 邮箱 / UUID / 保存时间，一键「切换到此账号」（当前账号按钮禁用并标记「使用中」）
- **保存快照**：输入名字即可把当前登录凭据存为快照，支持勾选覆盖同名快照（`FORCE=1`）
- **重启 Qoder**：登录态不丢的兜底按钮
- **操作日志**：每次操作展示 `qoder-switch.sh` 的真实输出（终端风格，含成功/失败标记）

### 安全设计

- 服务**仅监听 127.0.0.1**，不对外网/局域网暴露
- 快照名白名单校验（服务端 + 页面双侧）：仅允许中文/字母/数字/下划线/连字符，1~32 位，杜绝路径穿越
- 所有写操作均带确认弹窗；切换前脚本仍会自动回存当前账号
- 界面只是脚本的前端，逻辑以 `qoder-switch.sh` 为唯一真相源

## 七、分析方式说明

- 解包 `Qoder-Installer-mac-arm64.zip` → 提取内嵌 `Qoder-0.4.8-mac-arm64.zip` → `asar extract` 得到完整源码
- 主进程字符串表重度混淆，静态分析定位到关键 IPC 面（`auth.readState` / `auth.readUserToken`）、SDK 认证机制（`@qoder-ai/qoder-agent-sdk` 的 jobToken payload）与登录 UI 流程
- 关键结论均与**本机运行态交叉验证**：主进程日志（`[Auth]` 系列）、实际磁盘文件、钥匙串条目、以及两次主动实验（同内容原子重写测热加载 / 重启测恢复）
- 实验记录：2026-10-15 16:27 重写实验（无热加载）、16:30 重启实验（恢复成功、状态文件刷新）
- 临时文件 `/tmp/qoder-analysis/`（安装包解包 + asar 源码）可自行清理
