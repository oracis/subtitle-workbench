# -*- coding: utf-8 -*-
"""test_dragdrop.py - 模拟一次真实的 WM_DROPFILES 拖放，验证
subtitle_workbench.enable_drag_drop 的子类化 WndProc + DragQueryFile 链路。

做法：按 shell 认可的配方（GMEM_MOVEABLE|GMEM_ZEROINIT + DROPFILES 头）
造一个 HDROP，SendMessageW 发给子类化后的顶层窗口——与资源管理器
真实拖入走同一条消息路径（wParam=HDROP）。
"""
import ctypes
import os
import queue
import sys
import tkinter as tk
from ctypes import wintypes

sys.path.insert(0, r"D:/subtitle-workbench")
import subtitle_workbench as swb

kernel32 = ctypes.windll.kernel32
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalFree.restype = wintypes.HGLOBAL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

GMEM_MOVEABLE_ZEROINIT = 0x42


def make_hdrop(paths):
    """构造 shell 认可的 HDROP（GMEM_MOVEABLE|GMEM_ZEROINIT，只写
    pFiles=20 与 fWide=1，字符串列表为宽字符双 NUL 结尾）。"""
    blob = b"".join(p.encode("utf-16-le") + b"\x00\x00" for p in paths) + b"\x00\x00"
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE_ZEROINIT, 20 + len(blob))
    ptr = kernel32.GlobalLock(h)
    ctypes.memmove(ptr, b"\x14\x00\x00\x00", 4)        # pFiles = 20
    ctypes.memmove(ptr + 16, b"\x01\x00\x00\x00", 4)   # fWide = TRUE
    ctypes.memmove(ptr + 20, blob, len(blob))
    kernel32.GlobalUnlock(h)
    return h


root = tk.Tk()
root.title("dragdrop test")
root.geometry("320x160")

got = []
hwnd = swb.enable_drag_drop(root, lambda paths: got.extend(paths))
print("registered hwnd =", hwnd)

test_files = [r"D:\subtitle-workbench\subtitle_workbench.py",
              r"D:\subtitle-workbench\config.json"]
h = make_hdrop(test_files)

user32 = ctypes.windll.user32
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW(hwnd, swb._WM_DROPFILES, h, 0)
kernel32.GlobalFree(h)

root.update()
print("handler received:", got)

# 顺手验证 App.on_drop_files 的过滤逻辑（不走消息循环，直接调回调）
print("probe filter logic:")


class FakeListbox:
    def __init__(self):
        self.items = []
    def get(self, a, b):
        return tuple(self.items)
    def insert(self, idx, item):
        self.items.append(item)


class ProbeApp(swb.App):
    def __init__(self):
        self.listbox = FakeListbox()
        self.log_queue = queue.Queue()


probe = ProbeApp()
# 造两个假音频 + 一个不支持的 .spec + 一个含音频的目录
import tempfile
tmp = tempfile.mkdtemp(prefix="droptest-")
fa = os.path.join(tmp, "a.m4a")
fb = os.path.join(tmp, "b.mp3")
sub = os.path.join(tmp, "sub")
os.makedirs(sub)
fc = os.path.join(sub, "c.flac")
for f in (fa, fb, fc):
    with open(f, "wb") as fh:
        fh.write(b"\x00")
probe.on_drop_files([fa, fb, fc,
                     r"D:\subtitle-workbench\字幕工作台.spec",
                     sub])
print("after on_drop_files:", probe.listbox.items)

ok = (got == test_files
      and probe.listbox.items == [fa, fb, fc])
print("PASS" if ok else "FAIL")
os._exit(0 if ok else 1)  # 绕过解释器收尾（wndproc 子类化在退出期会被拆）
