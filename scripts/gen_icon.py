#!/usr/bin/env python3
"""生成桌面端/网页图标（512×512 PNG，纯标准库实现，无第三方依赖）。

设计：深色圆角方块底 + 蓝紫渐变 + 三层白色圆角横条（多账号/多平台的抽象表达）
+ 右上角点缀账号切换圆点。重跑覆盖 desktop/build/icon.png。
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

SIZE = 512
OUT = Path(__file__).resolve().parents[1] / "desktop" / "build" / "icon.png"


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_rect_sdf(x, y, cx, cy, hw, hh, r):
    """有符号距离：<0 在圆角矩形内。"""
    dx, dy = abs(x - cx) - (hw - r), abs(y - cy) - (hh - r)
    dx, dy = max(dx, 0.0), max(dy, 0.0)
    outside = math.hypot(dx, dy)
    inside = min(max(dx, dy), 0.0)
    return outside + inside - r


def render() -> bytes:
    # 顶色 #1d4ed8 → 底色 #7c3aed 的对角渐变
    top, bottom = (29, 78, 216), (124, 58, 237)
    rows = []
    for y in range(SIZE):
        row = bytearray()
        for x in range(SIZE):
            # 圆角方形底（角半径 112），边缘 1px 抗锯齿
            d = rounded_rect_sdf(x + .5, y + .5, SIZE / 2, SIZE / 2, SIZE / 2, SIZE / 2, 112)
            if d <= -0.5:
                alpha = 255
            elif d >= 0.5:
                alpha = 0
            else:
                alpha = int(255 * (0.5 - d))
            # 对角渐变 + 轻微纵向提亮
            t = (x + y) / (2 * SIZE)
            base = lerp(top, bottom, t)
            if y < SIZE * 0.28:
                base = tuple(min(255, c + 16) for c in base)
            # 三层白色圆角横条（宽度递增，表示账号层叠）
            bar = 0
            for i, w in enumerate((118, 158, 198)):
                cy = SIZE * (0.345 + i * 0.155)
                bd = rounded_rect_sdf(x + .5, y + .5, SIZE / 2, cy, w, 21, 21)
                if bd < 0:
                    bar = 255
                    break
            # 右上角切换圆点（accent 绿）
            dot = math.hypot(x + .5 - SIZE * 0.745, y + .5 - SIZE * 0.255) <= 33
            if bar:
                r, g, b = 255, 255, 255
                a = 235
            elif dot:
                r, g, b = 34, 197, 94
                a = 255
            else:
                r, g, b = base
                a = alpha
            row += bytes((r, g, b, a))
        rows.append(bytes(row))

    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(render())
    print(f"icon written: {OUT} ({OUT.stat().st_size} bytes)")
