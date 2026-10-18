// Trae 自动签到扩展
// 原理(DAILY_CHECKIN_ANALYSIS.md):签到接口有 mssdk 签名墙,脚本无法直连;
// 但应用注册了 workbench.action.icubeClaimCheckinCredits 命令,进程内执行自动带签名。
// 本扩展在启动后轮询签到快照,就绪且未签时自动领取(配合 SignCenter 切号即实现全账号签到)。
'use strict';
const vscode = require('vscode');
const fs = require('fs');
const path = require('path');
const os = require('os');

const LOG_FILE = path.join(os.homedir(), '.trae-cn', 'autocheckin.log');
const POLL_MS = 5000;
const MAX_ATTEMPTS = 40; // 约 200 秒(等待账号恢复/状态就绪)

function log(msg) {
  try {
    fs.appendFileSync(LOG_FILE, `${new Date().toISOString()} ${msg}\n`);
  } catch (e) { /* 日志失败不影响主流程 */ }
}

async function getSnapshot() {
  try {
    return await vscode.commands.executeCommand('workbench.action.icubeGetCheckinCreditsSnapshot');
  } catch (e) {
    log(`getSnapshot error: ${e}`);
    return null;
  }
}

async function tryClaim(attempt) {
  const snap = await getSnapshot();
  if (!snap || typeof snap !== 'object') {
    log(`attempt=${attempt} snapshot unavailable`);
    return false;
  }
  log(`attempt=${attempt} snapshot=${JSON.stringify(snap).slice(0, 220)}`);
  if (snap.readiness !== 'ready') {
    // 状态未就绪:每 4 次尝试主动刷新一次状态
    if (attempt % 4 === 0) {
      try {
        await vscode.commands.executeCommand('workbench.action.icubeRefreshCheckinCreditsStatus');
        log('status refreshed');
      } catch (e) { log(`refresh error: ${e}`); }
    }
    return false;
  }
  // enable=true 且今日未签 → 领取
  if (snap.enable === true && snap.checkedIn === false && snap.didCheckedIn !== true) {
    try {
      const r = await vscode.commands.executeCommand('workbench.action.icubeClaimCheckinCredits', 'extension');
      log(`claim executed, result=${JSON.stringify(r)}`);
    } catch (e) {
      log(`claim error: ${e}`);
      return false;
    }
    // 领取后再确认一次状态
    const after = await getSnapshot();
    log(`after claim: checkedIn=${after && after.checkedIn}`);
    return true;
  }
  // enable=false(未开放/非 CN/非 marscode) 或已签过 → 完结
  log('no claim needed (enable=' + snap.enable + ' checkedIn=' + snap.checkedIn + ')');
  return true;
}

function activate(ctx) {
  log(`activated, app=${vscode.env.appName || '?'}, version=${vscode.version}`);
  let attempt = 0;
  const timer = setInterval(async () => {
    attempt += 1;
    try {
      const done = await tryClaim(attempt);
      if (done || attempt >= MAX_ATTEMPTS) {
        clearInterval(timer);
        log(`finished (attempt=${attempt}, done=${done})`);
      }
    } catch (e) {
      log(`loop error: ${e}`);
    }
  }, POLL_MS);
  ctx.subscriptions.push({ dispose: () => clearInterval(timer) });
}

module.exports = { activate };
