# Trae 每日签到领积分模块 — 逆向分析报告

> 分析对象：Trae CN 1.107.1（`/Applications/Trae CN.app`，Bundle `cn.trae.app`）
> 分析方式：静态分析 `out/main.js`、`out/vs/workbench/workbench.desktop.main.solo-lite.js`、`node_modules/@byted-icube/*`、`@byted-solo/commercial-config-sdk`，并用本机真实账号实测交叉验证
> 分析时间：2026-10-15

---

## 一、结论速览

| 项 | 结论 |
|---|---|
| 是否有签到 | 有。每日签到领「积分（credits）」，**仅 Trae CN 可用**（国际版不可） |
| 签到接口 | `POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/{status,claim}` |
| 鉴权 | `Authorization: Bearer <accessToken>` + `x-device-id` 等 UG 设备头 |
| 能否脚本直连 | **不能**。请求必须经 Trae 自带的 ttnet + mssdk(BDMSSdk) 签名；普通 HTTP 客户端被网关拒为 `code:1001 authenticate failed`（已实测，与应用日志 `200 OK` 同时刻对照证明） |
| 自动化可行路径 | 调用应用已注册的 VS Code 命令 `workbench.action.icubeClaimCheckinCredits`（在应用进程内执行，自动带签名） |

---

## 二、功能定位与触发链路

签到功能由「商业化（commercial）」体系承载，分布在三处代码：

1. **数据模型** `@byted-solo/commercial-config-sdk`：定义 `CheckinCreditsRaw`。
2. **UI 组件** `@byted-icube/solo-lite/dist/9178.b944e6f7.mjs`：侧边栏底部 banner（`dailyCheckinBannerRenderer`）+ 弹窗（`commercialBannerPopup`）。
3. **服务层** `out/main.js`：`CNCommercialService.claimCheckin()` 与底层 `_requestCheckinCredits()`。

完整触发链路：

```
用户点 banner「签到」按钮
   │  (React, 9178.mjs)
   ▼
u.My().resolve(CNCommercialService).claimCheckin("sidebar_banner")
   │  (solo-lite 商业化服务)
   ▼
desktop-modules UI：actionId "claim_checkin"
   │  → IICubeEntitlementService.claimIdeCheckinCredits("banner_popup")
   ▼
sandbox → main  IPC: SANDBOX_TO_MAIN_INVOKE_CLAIM_CHECKIN_CREDITS
   │  = "vscode:sandbox::main-invoke-claimCheckinCredits"
   ▼
main.js: claimCheckinCredits()
   │  → _requestCheckinCredits("/trae/api/v2/ug/checkin_credits/claim","POST","checkin_claim")
   ▼
iCubeBaseTTNetService.fetchWithHeaders(...)   ← ttnet 原生栈 + mssdk 签名
   ▼
https://api.trae.cn/trae/api/v2/ug/checkin_credits/claim
```

状态查询走对称的另一条链路，UI 启动时自动轮询（应用日志可见每次启动都打一次 status）。

---

## 三、API 规格

### 前置守卫（`_requestCheckinCredits` 开头）

```js
if (this.productService.providerCode !== CN || n?.account.scope !== MARSCODE)
    throw new Error("Checkin credits is unavailable");
```

即：**只有 Trae CN（providerCode=CN）且账号 scope=marscode 才有签到**。本机 CN 账号 scope=`marscode` ✓。

### 1. 状态查询

| 项 | 值 |
|---|---|
| 方法 / 路径 | `POST /trae/api/v2/ug/checkin_credits/status` |
| Host | `https://api.trae.cn`（运行时从 boot 配置的 `ugApi` 取，实测即此） |
| Body | `{"req_source": 1}`（`li(productService)?2:1`，CN 走 1） |
| 超时 | 30s |
| 成功判定 | HTTP 200 且 `enable`、`checked_in` 均为 boolean |
| 响应字段 | `enable`(是否开放签到)、`checked_in`(今日是否已签)、`credits`、`extra_credits`，失败时带 `code`/`message` |

### 2. 领取签到

| 项 | 值 |
|---|---|
| 方法 / 路径 | `POST /trae/api/v2/ug/checkin_credits/claim` |
| Body | `{"req_source": 1}` |
| 成功判定 | HTTP 200 且（无 `code` 字段 或 `code===0`） |
| 成功副作用 | 广播 `MAIN_TO_SANDBOX_SEND_CHECKIN_CREDITS_INVALIDATED`，刷新套餐/积分快照 |

### 3. 请求头（`_buildRawHeaders` + `mixAuthorization` + `_applyUgDeviceHeaders`）

| 头 | 来源 |
|---|---|
| `Authorization` | `Bearer <accessToken>`（与 iCubeAuth 登录态同一 token，JWT，有效期 ~9 天，可自动续期） |
| `Content-Type` | `application/json` |
| `x-device-id` | `iCubeDeviceRegisterMainService.guaranteedDeviceId`（见下） |
| `x-device-brand` | `eventReportService.commonParams.device_model` |
| `x-device-type` | `commonParams.os_name`（mac） |
| `x-os-version` | `commonParams.os_version` |
| `x-app-version` | `commonParams.app_version`（1.107.1） |
| 可选 | PPE/BOE 环境：`x-tt-env` / `x-use-ppe` / `x-use-boe`（仅 `ai_assistant.request.env` 配置时） |

> 注：实际发出的还有 ttnet/mssdk 在原生层追加的签名头，JS 层不可见、也不可复刻。

---

## 四、数据模型（`commercial-config-sdk`）

```ts
CheckinCreditsRaw {
  enable?: boolean;          // 是否开放签到
  did_checked_in?: boolean;   // 是否已签（历史/快照语义）
  credits?: number;           // 签到可得积分
  extra_credits?: number;     // 额外积分
}
```

快照在本地还缓存为 storage.json 的 banner 状态键，例如：

```
solo-lite.commercial-banner.commercial:soloLite.banner:credits.dailyCheckin:user:<userId>:false
```

套餐/权益明文存于 `iCubeServerData://icube.cloudide`（如 `identityStr:"Pro"`）。

---

## 五、设备 ID（关键，已解密）

`x-device-id` 实取 `aha.device.device_id`，存于 `~/Library/Application Support/Trae CN/aha/TinyStorage`，用**同一套 byteCrypto** 加密。解密后：

```json
{"device_id_str":"4000000000000001","install_id_str":"4000000000000002","uuid":"XXXXXXXX-...","version":"1.2"}
```

- `device_id_str` = `4000000000000001`（示例值）—— **正好等于 `iCubeAuthInfo://icube-dc:4000000000000001` 键名后缀**（即切号器里 `icube-dc:` 后缀其实是「设备 ID」，不是 userId；切号器整组搬运不受影响）。
- `machineid` 文件（形如 `xxxxxxxx-...`）是 VS Code 本机 ID，**不是**签到用的设备 ID。

---

## 六、实测：脚本直连被 mssdk 拦截（决定性证据）

用本机真实 token + 正确 device_id_str，以标准库 `urllib` 直连状态接口：

```
POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/status
  Authorization: Bearer eyJhbGciOiJSUzI1...（JWT 未过期）
  x-device-id: 4000000000000001（示例值）
  body: {"req_source":1}

→ HTTP 200  {"checked_in":false,"code":1001,"enable":false,
            "message":"...not able to authenticate you..."}
```

**同时刻应用自身日志**（`logs/20261015T172651/main.log`）：

```
17:26:54.004  ttnet fetch request  https://api.trae.cn/trae/api/v2/ug/checkin_credits/status
17:26:55.523  ttnet fetch response https://api.trae.cn/.../status  200 OK
```

即：**同一个 URL、同一个 token、同一个 device-id**，应用（经 ttnet/mssdk）拿 `200 OK`，脚本拿 `1001 authenticate failed`。差别唯一在于 ttnet 原生栈的 mssdk 签名（`AhaEnableBDMSSdk: true`，见 `aha/Remote_State`）。

`signRequest` 在 JS 层的命中全部是无关的「solo-remote-file 设计资源」处理 —— **签名不在 JS，无法用 Python/Node 复刻**。原生签名实现于 `@aha-kit/net` / `modules/ai-agent/libharness.dylib` 等。

> 因此：**写一个脱离 Trae 进程的独立签到脚本不可行**（除非逆向并复刻 mssdk 原生签名，工作量巨大且随版本变）。

---

## 七、自动化可行路径：调用已注册命令

应用注册了 4 个 VS Code 命令（`workbench.desktop.main.solo-lite.js` 中 `registerCommand`）：

| 命令 ID | 作用 |
|---|---|
| `workbench.action.icubeClaimCheckinCredits` | **签到领积分**（→ `claimIdeCheckinCredits(source)`） |
| `workbench.action.icubeRefreshCheckinCreditsStatus` | 刷新签到状态 |
| `workbench.action.icubeGetCheckinCreditsSnapshot` | 取本地签到快照 |
| `workbench.action.icubeReportCheckinCreditsShown` | 上报 banner 展示 |

因为这些命令在**应用进程内执行**，自动经 ttnet 签名，所以**不受 mssdk 拦截**。触发方式：

1. **命令面板**（手动）：`Cmd+Shift+P` → 搜索命令名执行。
2. **键绑定**（半自动）：在 `keybindings.json` 给该命令绑一个快捷键，按一下即签。
3. **小扩展**（全自动）：写一个极简 Trae/VS Code 扩展，启动时 `vscode.commands.executeCommand('workbench.action.icubeClaimCheckinCredits')`，配合每日定时（扩展内 `setInterval` 或系统 cron 唤醒应用）。这是**唯一可靠的全自动路径**。
4. 触发后，若 `enable=true && checked_in=false`，应用会自动完成领取并把新积分写入 `iCubeServerData` 快照。

---

## 八、附：本次实测的账号数据形态（已全部脱敏）

| 字段 | 值（示例） |
|---|---|
| userId | 1000000000000001 |
| device_id_str | 4000000000000001 |
| accessToken | JWT，`exp` = 签发后约 14 天 |
| refreshToken 有效期 | 签发后约 6 个月 |
| 套餐 | Pro |
| 签到可用性 | enable 待应用侧确认（脚本被拦，无法独立判定） |

---

## 九、合规与风险提示

- 签到积分属 Trae 运营权益，自动化签到是否合规以 Trae 用户协议为准；本报告仅做技术分析。
- 自动化首选「应用内命令」而非「绕签名直连」——后者既不可行也可能违反服务条款。
- token / device_id 属账号凭据，泄露等同账号失窃，**切勿外传**。
