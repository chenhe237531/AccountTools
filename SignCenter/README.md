# 多平台自动签到中心（SignCenter）

AccountTools 的统一 Web 控制台：把 `Workbuddy/`、`Trae/`、`Qoder/` 三套切号器聚合到一个
FastAPI 服务里，提供**账号切换 + 自动签到 + 签到日志**的一站式深色界面。

## 快速开始

```bash
cd SignCenter
./run.sh                 # 首次自动创建 .venv 并安装依赖，然后启动
# 或手动：
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py --port 8800
```

启动后访问 <http://127.0.0.1:8800>（仅监听本机回环，不对外暴露）。

## 功能

| 功能 | 说明 |
|---|---|
| 平台卡片 | WorkBuddy / Trae CN / Qoder，各带启停开关、当前账号、快照列表 |
| 添加账号 | 在桌面应用登录后点「+ 添加账号」，保存当前登录态为快照 |
| **导入 JSON** | WorkBuddy 卡片「⬆ 导入 JSON」：导入外部导出的登录凭证（access/refresh token），账号直接经官方 API 签到，**不切换、不重启桌面应用** |
| 切换账号 | 点账号 chip：WorkBuddy 热加载秒切；Trae/Qoder 退出应用→换凭据→重启 |
| 立即签到 | 卡片蓝色按钮：**只签当前登录的这个账号**（已是当前账号，不重启应用，秒级完成） |
| 全部签到 | 卡片灰色按钮：快照账号依次切换签到，**导入账号 API 直连**，完成后自动切回原账号 |
| 全部平台签到 | 底部蓝色按钮：对所有已启用平台执行「全部签到」 |
| 自动签到 | 每天到设定时间（默认 05:20）自动全部签到；「自动补签」支持当天补跑 |
| 签到记录 | 底部面板滚动显示最近 100 条记录，持久化在 `data/logs.jsonl` |
| **成长任务** | WorkBuddy 卡片紫色按钮「成长任务 N/M」：查看[成长中心](https://www.workbuddy.cn/profile/growth-center)任务进度，勾选后**后台自动完成**获得积分/能量（详见下文） |

## 凭证导入（WorkBuddy token 直连账号）

WorkBuddy 的 access/refresh token 可直连 `www.workbuddy.cn` 官方签到 API（Bearer 鉴权，
本机实测 WAF 放行），因此外部导出的登录凭证无需进入桌面应用即可签到：

1. 点 WorkBuddy 卡片「**⬆ 导入 JSON**」，选择凭证文件（支持单对象或数组、
   平铺 `access_token/refresh_token` 或 `auth_raw.auth.*` 嵌套格式，`Workbuddy/wb_import.py` 负责解析）；
2. 导入时自动**调官方接口验证**凭证有效性（失效则跳过并提示原因），uid 去重、重复导入即更新凭证；
3. 导入的账号以紫色虚线 chip 展示（悬停可见手机尾号/uid/凭证剩余天数），点击 chip 不会切换应用；
4. 「全部签到」/ 自动签到会覆盖这些账号：**直接 API 领取**，不切换桌面应用；token 过期时
   自动经 Keycloak 刷新端点续期（refresh_token 约每 28 天轮换一次，脚本自动落盘）。

凭证存于 `~/.workbuddy/token-accounts.json`（权限 600）。CLI 用法见
`Workbuddy/wb_import.py`（`import/list/status/checkin/remove`）。

> 注意：导入账号与桌面快照账号**不要混用同一账号**——桌面应用 daemon 刷新 token 会轮换
> refresh_token，导致导入的副本失效。池子账号只走导入直连即可。

## 成长任务自动化（WorkBuddy 专属）

WorkBuddy 卡片上的「成长任务 N/M」按钮（N=已领取任务数）：

1. **点击按钮** → 弹出任务列表：每行显示任务名、奖励（⚡积分/◇能量）、状态（未接受/进行中/可领奖/已领取）与**预估自动化耗时**；
2. **勾选任务**（可自动化的默认勾选，已领取/需手动的置灰）→ 点「后台执行勾选任务」；
3. **后台执行**：服务端线程执行，弹窗内实时滚动执行日志；期间卡片按钮呼吸闪烁，关闭弹窗不影响执行；
4. 执行完自动**领取全部已完成任务奖励**，并顺带处理**连续登录兑换**与**抽奖**。

### 执行通道（两条，均为真实路径）

| 通道 | 覆盖任务 | 原理 |
|---|---|---|
| wbipc 轻量通道 | 接受任务、领奖、连续登录兑换、抽奖 | 复用 `wb_checkin.py` 的 wbipc 客户端，让 WorkBuddy 应用进程用当前登录会话调成长中心官方 API，秒级完成 |
| 桌面端 CDP 通道 | 聊天 5 次、召唤专家/专家团、体验灵感/资料库/发现应用 | Playwright 连接 WorkBuddy 调试端口（`--remote-debugging-port=9223`），驱动真实产品使用，服务端按真实行为计数（全流程实测通过） |

**CDP 通道前置条件**：WorkBuddy 需带调试端口运行（一次性）：

```bash
cd AccountTools/Workbuddy && python3 desktop.py ensure
```

> 脚本会重启 WorkBuddy（与切号器 restart 同模式，登录态/聊天记录不受影响）。
> 执行 CDP 任务期间会真实操作应用界面，请勿同时使用该应用。
> 公众号关注、连接器授权等任务无法自动化，列表中标注「需手动」。

### 成长任务 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/workbuddy/growth/status` | 任务概览（60s 缓存）：进度/能量/等级/任务列表 |
| POST | `/api/workbuddy/growth/run` | 后台执行 `{tasks: [task_code…]}` |
| GET | `/api/workbuddy/growth/job` | 执行进度（steps 日志流 + 结果） |

CLI 用法：`python3 wb_growth.py status` / `python3 wb_growth.py run chat_5 expert_5`

## 「签到」的原理（真实领取，2026-10-16 重构）

早期版本假设「每日奖励随登录态恢复自动发放」——**实测该假设不成立**：WorkBuddy 与 Trae CN
的每日积分都需要主动调用领取接口，仅切换登录态不会发放。现改为各平台走**真实领取路径**：

### WorkBuddy —— wbipc 通道让应用代领

逆向 WorkBuddy 5.8.2 发现：主进程在 Unix socket 上运行 **wbipc broker**（NDJSON 帧 +
HMAC-SHA256 票据握手，endpoint/ticket 在 `~/.workbuddy/wbipc/endpoint.json`），暴露
`wb.request` pipe 的 `http.fetch` 方法——**应用进程用它自己的当前会话鉴权头发任意后端请求**。
`wb_checkin.py` 通过该通道调用官方签到接口（无需碰 token 加密、无需复刻 Turing 风控签名）：

- `POST /v2/billing/meter/daily-checkin` 领取（重复领取返回 `code=10001`，幂等）
- `POST /v2/billing/meter/checkin-activity-status` 状态查询
- 实测：单账号 100 积分/天，3 账号 11 秒内全部领完（含热切换）

### Trae CN —— 应用内扩展自动领取

签到接口 `POST api.trae.cn/.../checkin_credits/claim` 有 mssdk 签名墙，脚本直连必被拒
（详见 `Trae/DAILY_CHECKIN_ANALYSIS.md`）。唯一可靠路径是执行应用注册的命令
`workbench.action.icubeClaimCheckinCredits`（进程内自动带签名）。因此安装了极简扩展
`trae-autocheckin`（源码在本目录，安装于 `~/.trae-cn/extensions/trae-autocheckin/`）：

- 应用启动后轮询签到快照，`readiness=ready && enable && 未签` 时自动执行领取
- 领取/跳过记录写 `~/.trae-cn/autocheckin.log`，SignCenter 据此确认结果
- 实测：扩展激活 5 秒内领取成功（200 积分 + 100 额外积分）

> 注意：扩展只在**应用启动**时激活。SignCenter 对 Trae 启用了 `always_relaunch`
> （同号也重启）确保扩展运行。**Trae 正在运行且无法优雅退出时（如 Agent 会话占用中），
> 签到流程会安全中止**，不会强杀应用。

### Qoder

目前仅切换刷新登录态（未发现独立领取接口；如有请通过钩子接入）。

### 自定义钩子（可选，优先级最高）

在 `data/config.json` 为平台配置 `checkin_command` 后，将优先执行钩子而非上述内置领取：

```json
{
  "platforms": {
    "qoder": {
      "enabled": true,
      "checkin_command": "curl -s -X POST 'https://example.com/signin' -H \"Authorization: Bearer $(cat ~/.some/token)\""
    }
  }
}
```

钩子内可用环境变量：`$SIGN_PLATFORM`（平台 ID）、`$SIGN_ACCOUNT`（账号快照名）。
钩子命令返回码为 0 视为签到成功。

## 架构

```
SignCenter/
├── main.py            # FastAPI 后端：平台适配层 + 签到编排(真实领取) + 调度器 + API
├── wb_checkin.py      # WorkBuddy wbipc 客户端：status/claim 单行 JSON 输出
├── trae-autocheckin/  # Trae CN 自动签到扩展源码(安装于 ~/.trae-cn/extensions/)
├── static/index.html  # 前端单页（原生 JS，轮询 /api/overview）
├── data/              # 运行时数据：config.json、logs.jsonl
├── requirements.txt
└── run.sh             # 一键启动
```

设计原则与三个子工具一致：

- **单一逻辑源**：所有切换/保存动作仍调用各目录下的核心脚本
  （`wb-switch.sh`、`qoder-switch.sh`、`trae_switch.py`），本服务只做编排与状态聚合。
- **仅监听 127.0.0.1**，快照名白名单校验，不引入新的凭据存储。
- 快照仍由各平台自己目录管理（`~/.workbuddy/...`、`~/.qoder/...`、`~/.trae-switcher/...`），
  本工具的「删除账号」只删快照文件，不动应用内登录态。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/overview` | 全平台状态聚合（卡片数据 + 今日进度 + 自动签到设置） |
| GET | `/api/logs?limit=100` | 最近签到/切换日志 |
| POST | `/api/platform/{pid}/toggle` | 启用/停用平台 `{enabled}` |
| POST | `/api/platform/{pid}/save` | 保存当前账号快照 `{name, force?}` |
| POST | `/api/platform/{pid}/switch` | 切换账号 `{name}` |
| POST | `/api/platform/{pid}/delete` | 删除快照 `{name}` |
| POST | `/api/platform/{pid}/checkin` | 平台签到 `{name}`=指定账号（当前账号签到）；`{}`=该平台全部账号轮流签到 |
| POST | `/api/platform/workbuddy/import` | 导入凭证 JSON `{data: "<文件内容>"}`（验证有效后入池，uid 去重） |
| POST | `/api/checkin/all` | 全部已启用平台签到 |
| POST | `/api/settings` | 修改自动签到设置 |

`pid` 取值：`workbuddy` / `trae` / `qoder`。

## 注意事项

- **Trae / Qoder 切换会退出并重启应用**（它们无热加载），签到前请保存好编辑器内容；
  WorkBuddy 支持热加载，秒级完成。
- **SignCenter 应独立运行，不要在 Trae 会话内操作 Trae 签到**：Agent 本身跑在 Trae 里时，
  应用无法退出（签到会安全中止）。每日 05:20 的自动签到通常发生在应用未运行时，
  届时流程会启动应用 → 扩展领取 → 正常完成。
- **WorkBuddy 签到要求应用在运行**（wbipc 通道依赖之）：`wb_checkin.py` 会在应用未运行时
  自动 `open -a WorkBuddy` 并等待最多 40 秒。
- 自动签到调度器在服务运行期间生效；如需真正无人值守（服务不开也跑），可用
  `launchd` 定时执行 `curl -X POST http://127.0.0.1:8800/api/checkin/all`，或把本服务注册为开机启动。
- refresh token 有有效期（约 30 天），长期不用的账号建议定期签到保活——这正是本工具的用途。
- Trae 默认管理 CN 版（`TRAE_PROFILE=cn`），如需国际版：`TRAE_PROFILE=intl .venv/bin/python main.py`。
- 合规提示：签到积分属各平台运营权益，自动化领取是否合规以各平台用户协议为准。
