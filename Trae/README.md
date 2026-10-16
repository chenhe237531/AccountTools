# Trae 快速切号器使用文档

> 适用版本：Trae 1.107.1（字节跳动 iCube 系 Electron 应用，VS Code 内核）
> 安装形态：`/Applications/Trae.app`（国际版，`com.trae.app`）与 `/Applications/Trae CN.app`（中国版，`cn.trae.app`）
> 核心脚本：本目录 `trae_switch.py` ｜ 图形界面：本目录 `server.py` + `index.html`

---

## 一、原理概述

Trae 的登录凭据不像多数 Electron 应用那样散落在多个文件里，而是**集中保存在 VS Code 状态文件 `storage.json` 的若干个键中**；并且 Trae 自己就内置了一个"把登录态搬到另一份安装"的功能（主进程 `migrateLoginState`），迁移的对象正是这几个键。

因此切号器设计为：

```
抽取每个账号的 4 类认证键做快照  +  先退出应用再原子写回 storage.json  +  重启加载  =  秒级切号
```

关键区别（与 WorkBuddy 那个切号器不同）：**Trae 无法热加载，必须重启**。原因见第二节第 3 点。

---

## 二、登录模块分析（静态分析 + 本机磁盘实测）

### 1. 核心会话文件

```
~/Library/Application Support/Trae CN/User/globalStorage/storage.json     # 中国版
~/Library/Application Support/Trae/User/globalStorage/storage.json        # 国际版
```

登录态由以下 4 类键组成（其余键为窗口/主题/遥测等无关状态）：

| 键 | 内容 | 是否加密 |
|---|---|---|
| `iCubeAuthInfo://icube.cloudide` | 用户信息 + `accessToken` / `refreshToken` / 有效期 / userId / 区域 | 是（byteCrypto） |
| `iCubeAuthInfo://usertag` | `{ userId: 地区标识 }`，如 `{"<userId>":"cn"}` | 是（byteCrypto） |
| `iCubeAuthInfo://icube-dc:<userId>` | 该账号对应的设备密钥对（与账号一一绑定） | 是（byteCrypto） |
| `iCubeServerData://icube.cloudide` | 套餐 / 权益信息（`identityStr` = Pro/Free 等） | 否，明文 JSON |

`icube-dc:` 键名里直接带着 userId，这是免解密辨识账号的快捷方式。

### 2. 加密格式（byteCrypto）

`iCubeAuthInfo::` 系列的值是 base64；解码后是 Trae 自研的 `byteCrypto` 信封（源码见 `out/main.js` 中 `vs/base/common/byteCrypto.js` 与 `vs/platform/iCubeAuth/electron-main/components/userStorage.js`）：

```
[6B 魔数 74 63 05 10 00 00] [32B 随机种子 seed] [AES-128-CBC 密文]
                                        │
      明文 = SHA-512(真实负载) ‖ 真实负载      （64 字节摘要用于完整性校验）
```

密钥派生（`Yce`）完全由文件内的 `seed` 与两条硬编码常量表决定：

```
buf = SHA-512(seed) ‖ (Sbox[i] ^ InvSbox[i])[0..63]      # 128 字节
buf[0..63] = SHA-512(buf)
aesKey = buf[0..16]   iv = buf[16..32]
```

**即：解密不依赖 macOS 钥匙串、不依赖机器，任何人拿到文件即可还原**（本脚本已把该算法纯 Python 移植，无第三方依赖）。

> 注意：`iCubeAuthInfo://icube-dc:<uid>` 里的 `privateKeyPEM` 是**二层混淆**的（PEM 头尾关键字被替换过），本工具只做整键搬运、不解析它，因此不受影响。

### 3. 为什么必须重启（关键结论）

主进程 `vs/platform/state/node/stateService.js` 中的 `fileStorage`：

```js
async doInit(){ this.storage = JSON.parse(await readFile(this.storagePath)); }   // 只读一次，进内存
getItem(k){ return this.storage[k]; }                                            // 之后只查内存
async doSave(){ ... 写入 storagePath ... }                                       // 仅由 setItem 触发
```

- 应用启动时把 `storage.json` **一次性读入内存**，之后所有读取都走内存副本；
- 全文件搜索确认：**没有任何针对 `storage.json` 的文件监视器**（区别于 WorkBuddy 的 `fs.watch` 热加载）；
- 更危险的是：应用运行中退出时会执行一次 `doSave()`，用内存副本把文件**覆盖回去**。

所以正确顺序只能是：**先退出 Trae → 再改文件 → 再启动**。若在运行中改文件，既不会生效，还可能被退出时的写回覆盖。

### 4. 账号数据天然隔离

Trae 按 Profile 目录存放数据（`Trae/` 与 `Trae CN/` 完全独立），因此本工具按 Profile 分别维护快照，互不干扰。工作区、插件、设置等均不随登录态变化。

### 5. 实测样例（本机解析结果）

| Profile | 用户名 | userId | 区域 | 套餐 | 联系方式 |
|---|---|---|---|---|---|
| Trae CN | 用户A（示例） | 1000000000000001 | CN | Pro | 138****0001 |
| Trae (国际版) | Demo User B（示例） | 2000000000000000001 | SG | Free | user.b@example.com |

> 两条记录的 `accessToken` 有效期约 14 天、`refreshToken` 有效期约 6 个月（具体日期随各自登录时间浮动）。

---

## 三、命令行用法

```bash
cd Trae

# 1) 在 Trae 中正常登录账号 A，存为快照
python3 trae_switch.py --profile cn save 主号

# 2) 在 Trae 中退出并登录账号 B，再存为快照
python3 trae_switch.py --profile cn save 小号

# 3) 之后一键切换（会自动退出 Trae → 回存当前账号 → 写回 → 重启）
python3 trae_switch.py --profile cn switch 主号

# 4) 查看状态
python3 trae_switch.py --profile cn list
python3 trae_switch.py --profile cn decode
```

完整命令：

| 命令 | 说明 |
|---|---|
| `list` | 列出所有快照与当前账号（含 userId / 用户名 / 套餐 / 剩余有效期） |
| `save <名字>` | 把当前登录账号存为快照（已存在需 `--force`） |
| `switch <名字>` | 切换到指定快照（自动退出 → 回存当前 → 写回 → 重启） |
| `delete <名字>` | 删除一个快照 |
| `restart` | 只重启应用 |
| `decode` | 解密并打印当前账号完整信息 |
| `state` | 输出机器可读 JSON（供图形界面使用） |

通用参数：`--profile cn|intl`（默认 `cn`）、`--json`；`switch` 另有 `--no-relaunch`。

### 脚本要点

- **切换前自动回存**：先退出应用，再把当前账号回存为快照（命中同 userId 则刷新，否则按邮箱/用户名新建），不丢会话；
- **必须等待退出完成**：脚本轮询主进程，确认退出（最多 30s 优雅退出 + 15s SIGTERM 兜底）后才写文件，避免被应用退出时的写回覆盖；
- **原子写**：临时文件 + `rename`，避免写一半被读到；
- **权限控制**：快照目录 700、快照文件 600；
- **不触碰加密逻辑**：认证键整组搬运，原样保留密文。

---

## 四、图形界面（本地控制台）

| 文件 | 作用 |
|---|---|
| `index.html` | 可视化界面（Profile 切换、账号卡片、状态条、操作日志） |
| `server.py` | 本地桥接服务：托管页面并把操作转发给 `trae_switch.py` |

### 启动

```bash
cd Trae
python3 server.py                 # 默认 http://127.0.0.1:8788，启动后自动打开浏览器
python3 server.py --port 9000 --no-open
```

无任何第三方依赖（Python 3 标准库），`Ctrl+C` 退出。

### 界面功能

- **Profile 切换**：顶部胶囊按钮在「Trae CN / Trae 国际版」之间切换，两者快照互相独立；
- **当前账号状态条**：用户名 / userId / 区域 / 套餐 / accessToken 剩余时间，并显示 Trae 是否正在运行；
- **快照卡片**：显示用户名、userId、区域套餐、登录方式（邮箱/手机号）、剩余有效期；一键「切换到此账号」，当前账号禁用按钮；
- **有效期提示**：refreshToken 剩余 < 7 天显示黄标，已过期显示红标；
- **保存快照**：输入名字即可保存当前账号，支持勾选覆盖；
- **重启 Trae**：维护按钮；
- **操作日志**：每次操作展示 `trae_switch.py` 的真实输出。

### 安全设计

- 服务**仅监听 127.0.0.1**，不对外网/局域网暴露；
- 快照名白名单校验（服务端 + 页面双侧）：中文/字母/数字/下划线/连字符，1~32 位，杜绝路径穿越；
- 切换操作带确认弹窗，并明确提示"会退出 Trae，请先保存文件"；
- 界面只是脚本的前端，逻辑以 `trae_switch.py` 为唯一真相源。

---

## 五、注意事项

1. **快照即凭据**：每个 `.json` 快照含有效的 accessToken / refreshToken，等同明文密码文件，**切勿放入网盘 / 代码仓库 / 聊天工具**。FileVault 磁盘加密 + 0600 权限是基本保障。
2. **切换会重启应用**：Trae 没有热加载，切换必然伴随一次退出与重启（约 5~15 秒）。**切换前请先保存编辑器中的文件**，建议先提交或暂存改动。
3. **token 有效期**：accessToken 约 14 天、refreshToken 约 6 个月。长期不用的账号，定期 `switch` 过去一次即可让应用自动续期，避免 refreshToken 失效。
4. **跨版本/跨区账号不通用**：`icube-dc` 设备密钥与账号绑定，且国际版（`api.trae.ai`）与中国版（`api.trae.cn`）账号体系不同 —— 不要在两个 Profile 之间互加速度快照。
5. **版本升级风险**：本结论基于 1.107.1 的 `storage.json` 结构。Trae 升级后若键名或 byteCrypto 版本号（魔数第 3~6 字节）变化，脚本会明确报错而不会写坏文件；届时对照新版 `out/main.js` 中的 `byteCrypto.js` / `userStorage.js` 更新即可。
6. **合规提示**：本方案为自己机器上的自有账号设计。若账号属企业管控，频繁换号是否合规以相关规定为准。

---

## 六、文件清单

| 文件 | 位置 | 说明 |
|---|---|---|
| `trae_switch.py` | 本目录 | 核心脚本（解密 + 快照 + 切换 + 进程控制） |
| `server.py` | 本目录 | 图形界面本地桥接服务 |
| `index.html` | 本目录 | 图形界面页面 |
| 账号快照 | `~/.trae-switcher/snapshots/<profile>/<名字>.json` | 各账号认证键快照 |
| 会话文件 | `~/Library/Application Support/Trae CN/User/globalStorage/storage.json` | 当前生效登录态（中国版） |
| 会话文件 | `~/Library/Application Support/Trae/User/globalStorage/storage.json` | 当前生效登录态（国际版） |

---

## 七、实测记录（2026-10-15，Trae CN 1.107.1）

| # | 测试项 | 结果 |
|---|---|---|
| 1 | 解密链路（byteCrypto 纯 Python 移植） | ✅ 两个 Profile 全部解出明文，SHA-512 完整性校验通过 |
| 2 | 切换全流程（退出 → 回存 → 写回 → 重启） | ✅ 重启后登录态存活，userId/套餐逐字段与基线一致 |
| 3 | 服务器侧验证 | ✅ 应用日志出现 `[TokenManager] Temp token refreshed and written to file`，即应用用写回的令牌向服务器刷新成功；随后 `getUserInfo` 被窗口反复调用 |
| 4 | 安全中止 | ✅ 应用未在预算内退出时，脚本拒绝写文件并中止（避免被运行中应用的退出写回覆盖） |
| 5 | 控制台 API 全链路 | ✅ `/api/state`、`/api/save`、`/api/switch`、`/api/delete` 全部通过 |
| 6 | 运行中应用的写回时机 | ✅ 实测应用运行时 mtime 持续更新（周期性落盘），证实"必须先退出再写回"的结论 |

**两点实测修正（已落实到脚本）：**

- Trae 优雅退出实测需要 **45~60 秒**（清理大量辅助进程），原 45 秒预算不够，已放宽为 75 秒优雅 + 30 秒 SIGTERM 兜底；
- `rename 覆盖已有文件` 在个别环境（文件策略代理/杀软）会被拒绝，已增加"直接写入"降级兜底；临时文件改为 finally 清扫 + 过期自动清理。

**尚未覆盖**：真正的 A↔B 双账号切换需要同一 Profile 登录过两个账号。机械链路已全部验证（应用恢复的就是我们写入的那份登录态），待你登录第二个账号、`save` 后即可实测。

---

## 八、分析方式说明

- 从 `TRAE-darwin-arm64.dmg` 与本机已安装的 `/Applications/Trae*.app` 定位 Electron 结构，确认其为 VS Code 内核（`out/main.js` + `out/vs/workbench/*`）；
- 全局检索 `iCubeAuthInfo` 命中主进程 `out/main.js`，顺藤摸到 `userStorage.js`（读写认证键）、`iCubeAuthService.js`（登录服务）、`stateService.js`（存储与是否热加载）、`byteCrypto.js`（加密格式）四个模块；
- 自研加密算法以纯 Python 复刻，并用本机真实 `storage.json` 交叉验证（解密结果与 SHA-512 完整性前缀均通过）；
- 热加载结论由 `stateService` 源码（无 watcher、内存副本、退出写回）推导，并与「Trae 官方只提供"迁移登录态"而非"跨实例共享登录态"」这一产品行为互相印证；
- DMG 未挂载安装，仅只读解析；分析过程未改动任何账号文件。
