// AccountTools 桌面端：Electron 壳负责拉起 Python 后端（同一 app/ 代码经
// PyInstaller 打包为 backend/agenttools-backend）并加载本机控制台页面。
//
// 形态：
//   打包后  resources/backend/agenttools-backend(.exe)
//   开发态  仓库根 .venv/bin/python -m app（无需打包即可跑桌面壳：
//           cd desktop && ACCOUNTTOOLS_DEV=1 npm start）
'use strict';

const { app, BrowserWindow, Menu, shell, dialog } = require('electron');
const { spawn } = require('child_process');
const net = require('net');
const path = require('path');
const fs = require('fs');
const http = require('http');

const DEV = process.env.ACCOUNTTOOLS_DEV === '1';
const BACKEND_NAME = process.platform === 'win32' ? 'agenttools-backend.exe' : 'agenttools-backend';
const REPO_ROOT = path.resolve(__dirname, '..');

let mainWindow = null;
let backend = null;
let backendPort = 0;

// ---------- 端口 ----------
function pickPort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

// ---------- 后端 ----------
function backendCommand() {
  if (DEV) {
    const venvPython = path.join(REPO_ROOT, '.venv', 'bin', 'python');
    const py = fs.existsSync(venvPython) ? venvPython : 'python3';
    return { cmd: py, args: ['-m', 'app'], cwd: REPO_ROOT };
  }
  const bin = path.join(process.resourcesPath, 'backend', BACKEND_NAME);
  return { cmd: bin, args: [], cwd: path.dirname(bin) };
}

function startBackend() {
  const { cmd, args, cwd } = backendCommand();
  backend = spawn(cmd, [...args, '--port', String(backendPort), '--no-open'], {
    cwd,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  const tag = `[backend pid=${backend.pid}]`;
  backend.stdout.on('data', (d) => console.log(tag, String(d).trimEnd()));
  backend.stderr.on('data', (d) => console.error(tag, String(d).trimEnd()));
  backend.on('exit', (code) => {
    console.error(tag, 'exited with', code);
    if (mainWindow && !mainWindow.isDestroyed()) {
      dialog.showErrorBox(
        '后端已退出',
        `AccountTools 后端进程异常退出（code=${code}）。\n` +
          (DEV ? '请检查 .venv 是否已安装依赖（run.sh）。' : '请重新安装应用后再试。')
      );
      app.quit();
    }
  });
}

function stopBackend() {
  if (!backend) return;
  const child = backend;
  backend = null;
  try {
    if (process.platform === 'win32') {
      // Windows 下杀整棵进程树
      spawn('taskkill', ['/pid', String(child.pid), '/f', '/t'], { windowsHide: true });
    } else {
      child.kill('SIGTERM');
    }
  } catch (e) {
    console.error('stop backend failed:', e);
  }
}

function waitBackend(timeoutMs = 90000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get({ host: '127.0.0.1', port: backendPort, path: '/api/health', timeout: 2000 }, (res) => {
        res.resume();
        resolve();
      });
      req.on('error', () => retry());
      req.on('timeout', () => { req.destroy(); retry(); });
    };
    const retry = () => {
      if (Date.now() - started > timeoutMs) reject(new Error('后端启动超时'));
      else setTimeout(tick, 400);
    };
    tick();
  });
}

// ---------- 窗口 ----------
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 940,
    minHeight: 640,
    backgroundColor: '#0a0e14',
    title: 'AccountTools',
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  Menu.setApplicationMenu(null);
  mainWindow.loadFile(path.join(__dirname, 'splash.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());

  // 外部链接交给系统浏览器，应用内只承载本机控制台
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url) && !url.startsWith(`http://127.0.0.1:${backendPort}`)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => { mainWindow = null; });
}

// ---------- 生命周期 ----------
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    try {
      backendPort = await pickPort();
      startBackend();
      await waitBackend();
      createWindow();
      mainWindow.loadURL(`http://127.0.0.1:${backendPort}/`);
    } catch (e) {
      dialog.showErrorBox('启动失败', `AccountTools 后端启动失败：${e.message}`);
      app.quit();
    }
  });

  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', stopBackend);
  process.on('exit', stopBackend);
}
