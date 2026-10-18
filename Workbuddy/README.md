# WorkBuddy 快速切号器使用文档

> 适用版本：WorkBuddy 5.7.6（腾讯 CodeBuddy 系 Electron 应用）
> 脚本位置：`~/.workbuddy/wb-switch.sh`（本目录为其备份副本）
> 图形界面：本目录 `index.html` + `server.py`（见「图形界面」章节）
> 成长任务自动化：本目录 `growth.py`（见「成长中心自动化」章节）

## 一、原理概述

WorkBuddy 的登录凭据整体存放在一个明文 JSON 会话文件中（仅 token 等敏感字段单独加密），且应用自带文件监视器——**外部替换该文件后会话会自动热加载，无需重启**。

因此切号器的设计为：

```
保存每个账号的会话快照  +  原子交换文件  =  秒级切号
```

## 二、会话文件机制（静态分析 + 本机磁盘实证）

### 1. 核心会话文件

```
~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
```

JSON 结构为 `{account, auth, accounts, allAccounts}`：

| 字段 | 含义 |
|---|---|
| `account` | 当前登录账号 |
| `auth` | 访问令牌（accessToken / refreshToken） |
| `accounts` | 最近账号列表 |
| `allAccounts` | 历史所有账号 |

加密情况：只有 `nickname`、`phoneNumber`、`accessToken`、`refreshToken` 四类字段使用 `$wbEncrypted` 信封加密（密钥存于 `~/.workbuddy/keyblob`，机器本地），其余字段（包括各账号的 uid/uin）均为明文。**整文件搬运完全不需要碰加密逻辑。**

### 2. 令牌有效期

- Keycloak 风格：accessToken 28 天、refreshToken 30 天有效
- daemon 会自动续期（文件中有 `lastRefreshTime` 字段）
- API 请求头：`Authorization: Bearer <accessToken>` + `X-User-Id`

### 3. 热加载机制

主进程 `FileAuthenticationStorage` 使用 `fs.watch` 监视 auth 目录，检测到外部改动后会打印日志 `Auth storage changed externally; reconciling`，随后重新读取并全进程广播新会话（daemon 日志中已验证 restore 流程正常）。

### 4. 账号数据天然隔离

每个账号的数据按 uid 分目录存储，切号互不影响、可共存：

- `~/.workbuddy/user-<uid>-personal/`
- `~/.workbuddy/security/<uid>/`
- webview 分区 `wb-webview-<uid>-*`
- `~/.workbuddy/connectors/<uid>/`

`device-id` 和 `qimei36` 为机器级共享，切号无需改动。

### 5. 实证案例

10 月 9 日切换账号时，应用登出时自动将旧会话备份为 `workbuddy-desktop.2026-10-09...info`——即另一个账号（uin 尾号 xxxx...）的完整可用会话，直接证明 **"整文件 = 一个账号"**。

## 三、切号器用法

```bash
~/.workbuddy/wb-switch.sh save 主号      # 每个账号正常登录一次后执行
~/.workbuddy/wb-switch.sh save 小号
~/.workbuddy/wb-switch.sh switch 小号    # 切换，通常 1~2 秒自动生效
~/.workbuddy/wb-switch.sh restart        # 万一没生效，重启兜底
```

完整命令列表：

| 命令 | 说明 |
|---|---|
| `save <名字>` | 把当前登录账号存为快照（已存在时需 `FORCE=1` 覆盖） |
| `switch <名字>` | 切换到指定账号（自动回存当前账号） |
| `list` | 列出所有快照与当前账号（含 uid/uin/保存时间） |
| `restart` | 重启 WorkBuddy 应用（热加载未生效时使用） |

### 脚本要点

- **切换前自动回存**：切换时先把当前账号回存成快照，不丢会话
- **原子写**：使用"临时文件 + rename"方式（与官方写法一致），避免写一半被读到
- **清理登出标记**：切换时自动删除 `.logged-out` 标记文件（存在该文件会导致会话被忽略）
- **权限控制**：快照目录权限 700，文件权限 600

## 四、注意事项

1. **快照即凭据**：每个 `.info` 快照含有效 refresh token，等同于明文密码文件，**切勿放入网盘 / 代码仓库**。FileVault 磁盘加密 + 0600 权限是基本保障。
2. **30 天过期**：长期不登录的账号 refresh token 会失效，定期 `switch` 过去一次再切回来即可续期。
3. **热加载待实测**：热加载行为是从代码（`file-authentication-storage.js` 的 watcher/reconcile 逻辑）推导的，未在登录态下实测，故保留 `restart` 兜底命令，实测一次即可确认。
4. **合规提示**：本方案为自己机器上的自有账号设计；若账号属企业管控，频繁换号是否合规以相关规定为准。

## 五、文件清单

| 文件 | 位置 | 说明 |
|---|---|---|
| `wb-switch.sh` | `~/.workbuddy/`（本目录有副本） | 切号脚本（核心逻辑） |
| `server.py` | 本目录 | 图形界面本地桥接服务 |
| `index.html` | 本目录 | 图形界面页面 |
| `growth.py` | 本目录 | 成长中心自动化（网页会话通道） |
| `desktop.py` | 本目录 | 桌面端 CDP 自动化（成长任务） |
| `wb_import.py` | 本目录 | 登录凭证导入 + token 直连签到（见「九」） |
| 会话快照 | `~/.workbuddy/account-switcher-snapshots/` | 各账号 `.info` 快照 |
| token 账号池 | `~/.workbuddy/token-accounts.json` | 导入的直连账号凭证（0600） |
| 会话文件 | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info` | 当前生效会话 |
| 加密密钥 | `~/.workbuddy/keyblob` | 敏感字段信封加密密钥（机器本地） |

## 六、图形界面（本地控制台）

命令行之上提供了一层可视化操作界面，由两部分组成：

| 文件 | 作用 |
|---|---|
| `index.html` | 可视化界面（快照卡片、当前账号状态、操作日志） |
| `server.py` | 本地桥接服务：托管页面并把按钮操作转发给 `wb-switch.sh` |

### 启动

```bash
cd Workbuddy
python3 server.py            # 默认 http://127.0.0.1:8787，启动后自动打开浏览器
python3 server.py --port 9000 --no-open   # 自定义端口 / 不自动打开
```

无任何第三方依赖（Python 3 标准库），`Ctrl+C` 退出。

### 界面功能

- **当前账号状态条**：实时显示 uid / uin；检测到 `.logged-out` 登出标记时顶部出现黄色警告
- **快照卡片**：每个账号一张卡片，显示 uid / uin / 保存时间，一键「切换到此账号」（当前账号禁用按钮）
- **token 保鲜提示**：快照超过 25 天未刷新显示「N 天未刷新」黄标，超过 30 天显示「token 可能已过期」红标
- **保存快照**：输入名字即可把当前账号存为快照，支持勾选覆盖同名快照（`FORCE=1`）
- **重启 WorkBuddy**：热加载未生效时的兜底按钮
- **操作日志**：每次操作展示 `wb-switch.sh` 的真实输出（终端风格，含成功/失败标记）

### 安全设计

- 服务**仅监听 127.0.0.1**，不对外网/局域网暴露
- 快照名白名单校验（服务端 + 页面双侧）：仅允许中文/字母/数字/下划线/连字符，1~32 位，杜绝路径穿越
- 所有写操作均带确认弹窗；切换前脚本仍会自动回存当前账号
- 界面只是脚本的前端，逻辑以 `wb-switch.sh` 为唯一真相源

## 八、成长中心自动化（growth.py）

自动化完成 [成长中心](https://www.workbuddy.cn/profile/growth-center) 的任务：接受任务、领取积分/能量奖励、兑换连续登录奖励、抽奖、开 Buddy 盲盒，并实时查看任务进度。

### 原理

- 成长中心全部走 `www.workbuddy.cn` 站内 HTTP API（`/v2/activity/growth/*`），与官网前端调用完全一致；
- 工具用 Playwright 驱动一个**专用 Chrome 持久会话**（`~/.workbuddy/growth-profile`），在轻量同源页内以官方前端相同方式 fetch，登录态保存在本地浏览器 profile，凭据不导出；
- **必须以有头模式运行**（窗口自动移到屏幕外）：workbuddy.cn 的 WAF 在 TLS/HTTP2 指纹层面识别无头浏览器，无头请求会被 401 并清除 session；
- 会话自愈：登录态意外失效时自动从 `~/.workbuddy/growth-session.json`（Cookie 引导文件，权限 600）重导入。

### 用法

```bash
# 首次使用:两种登录方式任选
python3 growth.py login                       # 打开浏览器窗口扫码/手机号登录
python3 growth.py login --cookies cookies.json  # 或导入 Cookie-Editor 插件导出的 JSON

# 日常使用
python3 growth.py status      # 查看任务列表/进度/能量/等级
python3 growth.py run         # 一键:接受任务 → 领奖 → 兑换连续登录 → 抽奖
python3 growth.py run --gacha # 同上,外加能量开 Buddy 盲盒
python3 growth.py claim       # 只领奖
python3 growth.py redeem      # 只兑换连续登录奖励(7/14/28 天档)
```

### 任务完成机制说明

任务进度由**服务端统计真实产品使用**得出（聊天/画布/专家/技能/模板等均为 WorkBuddy 客户端内的真实操作）：

- 工具负责：接受任务（接受后使用才会计数）、自动领奖、连续登录兑换、抽奖、盲盒；
- 产品体验类任务（和 AI 聊天 5 次、创建画布、召唤专家等）：在 WorkBuddy 客户端正常使用即自然累计，完成后运行 `run` 自动领奖;
- `run --try-chat`（实验）：尝试纯 API 发起真实对话（产品会话 API + ACP 协议）。云沙盒受服务端配额限制可能无法启动，失败属预期。

### 依赖

```bash
pip install playwright requests   # 系统需装有 Google Chrome
```

### 安全设计

- 登录态仅存于本地浏览器 profile 与 Cookie 引导文件（0600），不上传、不打印；
- 所有操作均为用户自有账号的站内官方 API，与网页端行为一致。

## 九、登录凭证导入与直连签到（wb_import.py）

把外部工具导出的 WorkBuddy 登录凭证 JSON（含 access/refresh token）导入为**token 直连账号**，
签到时直接调用 `www.workbuddy.cn` 官方 API 领取每日积分——**不切换、不重启桌面应用**，
与 wb-switch.sh 的整文件快照方案互补（实测 token 直连 WAF 放行；域名 codebuddy.cn/workbuddy.cn 均可用）。

### 用法（仅标准库）

```bash
python3 wb_import.py import 凭证.json   # 导入并验证(调官方接口确认有效,uid 去重,重复导入即更新)
python3 wb_import.py list               # 列出账号(脱敏,显示凭证剩余天数)
python3 wb_import.py status [uid|all]   # 查询签到状态
python3 wb_import.py checkin [uid|all]  # 直连领取每日积分(401 时自动经 Keycloak 刷新续期)
python3 wb_import.py remove <uid|昵称>   # 移除账号
```

### 输入格式

单对象或数组均可；平铺（`access_token/refresh_token`）或嵌套（`auth_raw.auth.accessToken`）均可：

```json
[{
  "uid": "…", "nickname": "…", "domain": "www.codebuddy.cn",
  "access_token": "eyJ…", "refresh_token": "eyJ…", "expiresAt": 1794518477000
}]
```

uid/昵称/手机号缺失时自动从 JWT claims（`sub`/`preferred_username`）补全。

### 存储与安全

- 凭据存 `~/.workbuddy/token-accounts.json`（0600），不上传、不打印；
- token 过期（401）时自动用 refresh_token 经 `<iss>/protocol/openid-connect/token` 续期并落盘
  （刷新会轮换 refresh_token，故**同一账号不要既导入直连又做桌面快照切换**——桌面 daemon 的刷新会使导入副本失效）；
- 签到积分为账号自有权益，合规性以平台用户协议为准。

### 与控制台(app)集成

签到中心 WorkBuddy 卡片的「⬆ 导入 JSON」按钮即调用本脚本：导入的账号以紫色虚线 chip 展示，
「全部签到」与每日自动签到均覆盖这些账号（API 直连，秒级）。

## 十、分析方式说明

- 用 REA 挂载 DMG 确认 Electron 结构并完成文件清单
- ASAR 全量解析因某文件触发解析器崩溃，改用 `@electron/asar` 解包 + 直接读源码
- 关键结论均与 daemon 运行日志、磁盘实际文件交叉验证
- DMG 已卸载，临时文件 `/tmp/wb-full` 和 `/tmp/wb-src` 可自行清理
