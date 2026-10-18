#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 签到客户端 —— 经应用自带 wbipc 通道让应用进程代发领取请求

原理(逆向自 WorkBuddy 5.8.2 app.asar):
  * WorkBuddy 主进程在 Unix socket 上运行 wbipc broker(NDJSON 帧 + HMAC-SHA256 票据握手)
  * endpoint 与 ticket 存于 ~/.workbuddy/wbipc/endpoint.json(每次应用启动刷新)
  * broker 暴露 wb.request pipe,方法 http.fetch:让应用用「当前登录会话」的
    鉴权头向 https://www.workbuddy.cn 发请求 —— 无需碰 token 加密,也无需复刻 Turing 签名
  * 签到接口:
      POST /v2/billing/meter/daily-checkin             领取(重复领取返回 code=10001)
      POST /v2/billing/meter/checkin-activity-status   状态查询

用法(stdout 输出单行 JSON,供控制台(app)解析):
  python3 wb_checkin.py status    # 查询当前账号签到状态
  python3 wb_checkin.py claim     # 领取今日积分
"""
import base64
import hashlib
import hmac
import json
import os
import socket
import sys
import time

HOME = os.path.expanduser("~")
ENDPOINT_JSON = os.path.join(HOME, ".workbuddy", "wbipc", "endpoint.json")
PIPE = "wb.request"
BACKEND_TIMEOUT = 50  # http.fetch 上游超时 50s


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _encode_transcript(prefix: str, t: dict) -> bytes:
    """与 app 内 encodeTranscript 一致:字段前 4 字节大端长度 + utf8。"""
    out = b""
    for f in [prefix, str(t["protocol"]), t["endpoint"], t["clientNonce"], t["serverNonce"]]:
        fb = f.encode()
        out += len(fb).to_bytes(4, "big") + fb
    return out


class WbipcClient:
    def __init__(self):
        if not os.path.isfile(ENDPOINT_JSON):
            raise RuntimeError("wbipc endpoint.json 不存在(WorkBuddy 未运行或版本过旧)")
        ep = json.load(open(ENDPOINT_JSON))
        self.sock_path, self.ticket = ep["endpoint"], ep["ticket"]
        self.ticket_id = hashlib.sha256(self.ticket.encode()).hexdigest()[:16]
        self.s = None
        self.f = None
        self._id = 0

    def connect(self):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(BACKEND_TIMEOUT + 10)
        self.s.connect(self.sock_path)
        self.f = self.s.makefile("rwb")
        # --- 握手: hello -> challenge -> prove -> hello_ack
        cn = _b64url(os.urandom(16))
        self._send({"type": "session_hello", "protocol_min": 1, "protocol_max": 1,
                    "client_nonce": cn, "ticket_id": self.ticket_id,
                    "client": {"kind": "script", "id": "signcenter", "version": "1.0"}})
        challenge = self._read()
        if challenge.get("type") != "session_challenge":
            raise RuntimeError(f"握手失败: {challenge}")
        t = {"protocol": 1, "endpoint": self.sock_path,
             "clientNonce": cn, "serverNonce": challenge["server_nonce"]}
        proof = _b64url(hmac.new(self.ticket.encode(),
                                 _encode_transcript("wbipc-c", t), hashlib.sha256).digest())
        self._send({"type": "session_prove", "client_proof": proof})
        ack = self._read()
        if ack.get("type") != "session_hello_ack":
            raise RuntimeError(f"认证失败: {ack}")

    def _send(self, obj):
        self.f.write(json.dumps(obj).encode() + b"\n")
        self.f.flush()

    def _read(self):
        line = self.f.readline()
        if not line:
            raise RuntimeError("wbipc 连接被关闭")
        return json.loads(line)

    def rpc(self, method, params, mode="call"):
        self._id += 1
        self._send({"method": method, "id": self._id, "params": params, "mode": mode})
        while True:
            r = self._read()
            if r.get("id") == self._id:
                return r

    def http_fetch(self, method, path, body=b"{}"):
        r = self.rpc("broker/GetPipe", {"pipe": PIPE})
        if "result" not in r:
            raise RuntimeError(f"GetPipe 失败: {r.get('error')}")
        channel = r["result"]["channel"]
        r2 = self.rpc(f"{channel}/http.fetch", {
            "method": method, "path": path,
            "headers": {"content-type": "application/json"},
            "body_b64": base64.b64encode(body).decode()})
        if "result" not in r2:
            raise RuntimeError(f"http.fetch 失败: {r2.get('error')}")
        res = r2["result"]
        payload = json.loads(base64.b64decode(res.get("body_b64", ""))) if res.get("body_b64") else {}
        return res.get("status"), payload

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def _wait_app(max_wait=40):
    """应用未运行时先启动并等待 wbipc endpoint 出现。"""
    if os.path.isfile(ENDPOINT_JSON):
        try:
            ep = json.load(open(ENDPOINT_JSON))
            if os.path.exists(ep["endpoint"]):
                return
        except Exception:
            pass
    for app in ("WorkBuddy", "WorkBuddy AI"):
        os.system(f'open -a "{app}" 2>/dev/null')
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            ep = json.load(open(ENDPOINT_JSON))
            if os.path.exists(ep["endpoint"]):
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("等待 WorkBuddy 启动超时(未出现 wbipc endpoint)")


def _call(path):
    _wait_app()
    c = WbipcClient()
    try:
        c.connect()
        return c.http_fetch("POST", path)
    finally:
        c.close()


def cmd_status():
    try:
        status, payload = _call("/v2/billing/meter/checkin-activity-status")
        data = payload.get("data") or {}
        print(json.dumps({
            "ok": status == 200 and payload.get("code") == 0,
            "active": data.get("active"),
            "today_checked_in": data.get("today_checked_in"),
            "streak_days": data.get("streak_days"),
            "total_credits": data.get("total_credits"),
            "daily_credit": data.get("daily_credit"),
        }, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))


def cmd_claim():
    """领取今日积分。code=0 成功;code=10001 视为今日已领(幂等)。"""
    try:
        status, payload = _call("/v2/billing/meter/daily-checkin")
        code = payload.get("code")
        if status == 200 and code == 0:
            d = payload.get("data") or {}
            print(json.dumps({"ok": True, "claimed": True, "credit": d.get("credit"),
                              "streak_days": d.get("streak_days"), "msg": "签到成功"}, ensure_ascii=False))
        elif code == 10001:
            print(json.dumps({"ok": True, "claimed": False, "already": True,
                              "msg": payload.get("msg", "今天已签到")}, ensure_ascii=False))
        else:
            print(json.dumps({"ok": False, "code": code,
                              "msg": payload.get("msg") or f"HTTP {status}"}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "status":
        cmd_status()
    elif cmd == "claim":
        cmd_claim()
    else:
        print(json.dumps({"ok": False, "error": "用法: wb_checkin.py status|claim"}, ensure_ascii=False))
