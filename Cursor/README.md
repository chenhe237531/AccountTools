# Cursor 快速切号器使用文档

> 适用版本：Cursor 3.21.9 / 3.24.18（Anysphere 出品，VS Code 系 Electron 应用，双向验证）
> 脚本位置：本目录 `cursor-switch.sh`（`server.py` 会优先使用 `~/.cursor/cursor-switch.sh` 原件）
> 图形界面：本目录 `index.html` + `server.py`（见「图形界面」章节）

## 一、原理概述

Cursor 的登录凭据全部集中存放在一个 SQLite 数据库的少数几行里（**明文**，不依赖 Keychain），但应用运行期间持有数据库连接且**不监听外部改动**——与 WorkBuddy 的热加载不同，Cursor 必须**退出 → 改库 → 重启**。

因此切号器的设计为：

```
行级凭据快照(只存十来行,几 KB)  +  退出 Cursor 后原子写库  +  自动重启  =  约 15 秒切号
```

与「整文件快照」方案相比的优势：state.vscdb 高达数百 MB（聊天记录、工作区状态都在里面），行级快照既不占空间，也**完全不动聊天/工作区数据**，多个账号共享同一份历史。

## 二、登录模块机制（静态分析 + 本机磁盘实证）

### 1. 凭据存储位置

```
~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
```

SQLite（WAL 模式），凭据在 `ItemTable` 表中，登录态完整行集（实测 Google 登录后）：

| 键 | 内容 |
|---|---|
| `cursorAuth/accessToken` | 访问令牌（JWT，明文） |
| `cursorAuth/refreshToken` | 刷新令牌（同为 JWT） |
| `cursorAuth/cachedEmail` | 登录邮箱 |
| `cursorAuth/cachedSignUpType` | 登录方式（Google / auth0 等） |
| `cursorAuth/cachedScopedProfile` | `{"displayName":"<显示名>"}` |
| `cursorAuth/stripeMembershipType` | 会员档位（free / pro / ultra…） |
| `cursorAuth/stripeMembershipAuthId` | 会员绑定的 authId |
| `cursorAuth/stripeSubscriptionStatus` | 订阅状态 |
| `glass.lastSignedInAuthId` | 最近登录的 authId（登出时为 `signed-out`） |

存储类型全部为 TEXT；`sqlite3` 直接可读写，无需处理二进制/加密。

### 2. 令牌机制

- 令牌为 JWT：`sub` = authId（如 `google-oauth2|user_xxxxxxxx`），`exp` = 签发后 **60 天**
- `iss: https://authentication.cursor.sh`，`aud: https://cursor.com`，`type: session`
- 应用在过期前自动用 refreshToken 刷新，**新令牌对会回写数据库**——所以切换时「回存当前账号」会把最新令牌存进快照
- 主进程启动时从 state.vscdb 读取 accessToken（`resolveStoredAccessToken`），渲染进程通过 `storageService` 同步使用

### 3. 为什么不能热切换

- 主进程 StorageMainService 打开数据库后长驻内存，无 `fs.watch` 监听外部修改（与 WorkBuddy 相反）
- SQLite WAL 模式下外部写入与运行中的应用连接存在锁冲突风险
- 因此脚本流程固定为：**退出 → 读终态 → 回存 → 行级替换 → 重启**

### 4. 服务端会话吊销机制（实测确认，重要）

- Cursor 界面里的 **Sign Out 会调用 `POST {backendUrl}/auth/logout` 服务端吊销当前 token**——已保存的快照随之全部失效（token 未过期也会被拒，刷新接口返回 `shouldLogout: true`）
- 而在 UI 里**登录另一个账号不会吊销任何已有会话**（登录流程不调用 logout 端点）
- 因此多账号初始化必须用脚本的 `signout-local`（本地登出，不通知服务器）来切换登录入口，**永远不要点界面里的 Sign Out**
- 应用在 token 未到期前不会主动刷新，也无法察觉 token 已被吊销——所以提供 `check` 命令做服务端存活检测

### 5. Keychain 与切号无关

macOS Keychain 仅存两样东西：Electron safeStorage 密钥（加密 `secret://` 前缀的**扩展**密钥，如 git token、BYOK API key）与崩溃上报相关项。账号凭据本身是明文行，整行搬运不碰任何加密逻辑。

### 6. 账号数据天然隔离

- 应用内大量状态按 `authId` 命名空间分槽存储（如 `cursor.glass.…​.auth0%7Cuser_xxx`），切换后各账号互不干扰
- 聊天记录（`cursorDiskKV` 表）、工作区数据为所有账号共享，不受切换影响
- `~/.cursor/`（CLI/agent 状态）、`storage.json`、`argv.json` 均无账号绑定
- 机器标识（`storage.serviceMachineId` 等）为机器级共享，无需改动

## 三、切号器用法

### 多账号初始化（重要：不要点 UI 的 Sign Out！）

```bash
./cursor-switch.sh save 主号        # 1. UI 登录账号 A 后保存
./cursor-switch.sh signout-local   # 2. 本地登出(不吊销 A 的服务端会话)
#                                    3. UI 登录账号 B
./cursor-switch.sh save 小号        # 4. 保存 B
```

之后即可自由往返切换（自动回存当前账号，单次约 15 秒）：

```bash
./cursor-switch.sh switch 小号
./cursor-switch.sh switch 主号
```

完整命令列表：

| 命令 | 说明 |
|---|---|
| `save <名字>` | 把当前登录凭据存为快照（Cursor 可运行中，只读不改动；已存在需 `FORCE=1` 覆盖） |
| `switch <名字>` | 切换到指定账号：退出 Cursor → 自动回存当前账号 → 写库 → 重启 |
| `signout-local` | **本地登出**：清空本机凭据但不调用服务端 `/auth/logout`，当前账号会话保持存活，用于安全登录下一个账号 |
| `check [名字...]` | 检测当前账号/快照 token 的服务端存活状态（向 `api.origin.cursor.com` 发只读握手，200=有效 / 401=已吊销） |
| `list` | 列出当前账号与所有快照（邮箱 / 会员 / token 剩余天数） |
| `current` | 仅显示当前登录账号详情 |
| `remove <名字>` | 删除指定快照 |
| `restart` | 重启 Cursor（不改动任何数据） |

环境变量：

| 变量 | 说明 |
|---|---|
| `FORCE=1` | save 覆盖同名快照；switch 在目标与当前同号时仍执行（用于恢复快照里的旧 token） |
| `NO_RELAUNCH=1` | switch / signout-local 后不自动重启（脚本化场景） |

### 脚本要点

- **同号保护**：目标账号与当前一致时直接跳过，不折腾退出/重启（`FORCE=1` 可强制）
- **切换前自动回存**：优先覆盖同 authId 的既有快照；没有匹配时自动按邮箱前缀新建 `auto-<名字>`，当前账号会话不会丢
- **安全检查**：写库前再次确认 Cursor 进程已退出，运行中一律拒绝写入；退出等待 15 秒失败即中止（不强杀，避免丢未保存编辑）
- **原子写**：快照用「临时文件 + rename」落盘；数据库操作为单事务 `DELETE + INSERT OR REPLACE`
- **权限控制**：快照目录 700，快照文件 600
- **BLOB 兼容**：快照格式对 bytes 值做 base64 标记，未来 Cursor 若把某行改成 BLOB 也不会坏

## 四、注意事项

1. **快照即凭据**：每个 `.json` 快照含有效 refreshToken，等同于明文密码文件，**切勿放入网盘 / 代码仓库**。FileVault + 0600 权限是基本保障。
2. **60 天有效期**：token 签发后 60 天过期，长期不用的账号记得定期 `switch` 过去一次再切回（应用会自动续期并回存新 token）。
3. **切换期间勿操作**：切换会退出 Cursor，若有未保存文件，退出可能被确认框阻塞——脚本会安全中止并提示，保存后重试即可。
4. **多设备/多账号**：切换只改本机登录态，不通知服务器，不影响任何账号在其他设备的会话。但**点 UI 的 Sign Out 会服务端吊销该账号的会话**（所有设备的该账号 token 一起失效），多账号场景请始终用 `signout-local`。
5. **失效排查**：切换后如遇 AI 功能报 401，先跑 `check`——若显示「已失效」，说明该账号快照的 token 被服务端吊销过（通常是误点了 UI 登出），需重新 UI 登录该账号并 `FORCE=1` 重存快照。
6. **合规提示**：本方案为自己机器上的自有账号设计；若账号属企业管控，频繁换号是否合规以相关规定为准。

## 五、文件清单

| 文件 | 位置 | 说明 |
|---|---|---|
| `cursor-switch.sh` | 本目录（可复制到 `~/.cursor/`） | 切号脚本（核心逻辑，内嵌 Python 处理 SQLite/JWT） |
| `server.py` | 本目录 | 图形界面本地桥接服务 |
| `index.html` | 本目录 | 图形界面页面 |
| 快照目录 | `~/.cursor/account-switcher-snapshots/` | 各账号凭据快照（JSON，0600） |
| 凭据数据库 | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` | 当前生效登录态 |

推荐把脚本安装到 `~/.cursor/` 后使用：

```bash
cp cursor-switch.sh ~/.cursor/ && chmod +x ~/.cursor/cursor-switch.sh
~/.cursor/cursor-switch.sh list
```

## 六、图形界面（本地控制台）

命令行之上提供了一层可视化操作界面，由两部分组成：

| 文件 | 作用 |
|---|---|
| `index.html` | 可视化界面（当前账号状态条、快照卡片、操作日志） |
| `server.py` | 本地桥接服务：托管页面并把按钮操作转发给 `cursor-switch.sh` |

### 启动

```bash
cd Cursor
python3 server.py            # 默认 http://127.0.0.1:8791，启动后自动打开浏览器
python3 server.py --port 9000 --no-open   # 自定义端口 / 不自动打开
```

无任何第三方依赖（Python 3 标准库），`Ctrl+C` 退出。

### 界面功能

- **当前账号状态条**：邮箱 / 显示名 / 会员档位 / token 到期倒计时，未登录时红点提示
- **运行提示条**：检测到 Cursor 运行中时显示蓝色提示（切换会先自动退出）
- **快照卡片**：每个账号一张卡片，含邮箱、显示名、会员徽章（free/pro/ultra 配色）、token 到期倒计时，一键「切换到此账号」（当前账号禁用）、「检查」（服务端存活检测，结果显示绿色「token 有效」/红色「已失效」徽章）或「删除」
- **token 保鲜提示**：剩余 ≤14 天黄标「N 天后到期」，≤0 天红标「token 已过期？」，卡片底部显示快照保存时间
- **保存快照**：输入名字即可保存，支持覆盖确认
- **维护面板**：「检测全部 token 有效性」「重启 Cursor」，以及**「本地登出（切换登录入口）」**——多账号初始化的核心入口，带红色警示说明
- **操作日志**：每次操作展示 `cursor-switch.sh` 的真实输出（终端风格，含成功/失败标记）

### 安全设计

- 服务**仅监听 127.0.0.1**，不对外网/局域网暴露
- 快照名白名单校验（服务端 + 页面双侧）：仅允许中文/字母/数字/下划线/连字符，1~32 位，杜绝路径穿越
- 切换/删除均带确认弹窗；切换前脚本仍会自动回存当前账号
- 界面只是脚本的前端，逻辑以 `cursor-switch.sh` 为唯一真相源

## 七、分析方式说明

- 用 REA 挂载 `Cursor-darwin-universal.dmg`（3.24.18），确认 Electron 结构；主进程代码 `out/main.js` 与渲染进程 `out/vs/workbench/workbench.desktop.main.js` 均未打包 asar，可直接检索
- 关键结论与**本机实际运行的 3.21.9 + 真实 Google 登录态**交叉验证：`cursorAuth/*` 全部 9 行、TEXT 类型、JWT 声明（sub/exp/iss）逐一核对
- 切换可行性在 SQLite 层演练（DELETE + INSERT OR REPLACE 往返）后落地为脚本
- DMG 分析完已卸载，临时文件 `/tmp/cursor-rea` 可自行清理
