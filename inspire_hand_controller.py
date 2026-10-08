#!/usr/bin/env python3
"""
Inspire Hand RH56DFTP — Professional Controller  v4.0
Dual-hand · Thread-safe buffer · Fully resizable · No black console

Run normally:   python inspire_hand_controller.py
No pythonw needed — console is freed automatically on Windows.
"""
# ──────────────────────────────────────────────────────────────────────────────
#  One euro Filter
# ──────────────────────────────────────────────────────────────────────────────
import time
import math

class OneEuroFilter:
    def __init__(self, freq, mincutoff=1.0, beta=0.0, dcutoff=1.0):
        self.freq = freq
        self.mincutoff = mincutoff
        self.beta = beta
        self.dcutoff = dcutoff
        self.x_prev = None
        self.dx_prev = None
        self.t_prev = None

    def __call__(self, x, t=None):
        if t is None: t = time.time()
        if self.t_prev is not None:
            delta = t - self.t_prev
            # Prevent division by zero if called too rapidly
            freq = 1.0 / delta if delta > 0.001 else self.freq
        else:
            freq = self.freq
        self.t_prev = t
        
        # Calculate derivative
        if self.x_prev is None:
            dx = 0.0
        else:
            dx = (x - self.x_prev) * freq
            
        # Filter derivative
        edx = self._alpha(freq, self.dcutoff)
        dx_hat = edx * dx + (1 - edx) * (self.dx_prev if self.dx_prev else 0.0)
        self.dx_prev = dx_hat
        
        # Filter value
        cutoff = self.mincutoff + self.beta * abs(dx_hat)
        ex = self._alpha(freq, cutoff)
        x_hat = ex * x + (1 - ex) * (self.x_prev if self.x_prev else x)
        self.x_prev = x_hat
        return x_hat

    def _alpha(self, freq, cutoff):
        tau = 1.0 / (2 * math.pi * cutoff)
        te = 1.0 / freq
        return 1.0 / (1.0 + tau / te)
# ──────────────────────────────────────────────────────────────────────────────
#  Windows: kill the console window immediately, before any Qt import
# ──────────────────────────────────────────────────────────────────────────────
import sys, platform, os
_IS_WIN = platform.system() == "Windows"

# ── CRITICAL: import mujoco BEFORE PyQt5 on Windows ──────────────────────────
# MuJoCo DLLs must load before Qt modifies the DLL search path.
# Skipping this causes WinError 1114 when creating mujoco.Renderer later.
_MJ_OK = False
try:
    import mujoco as _mj_pre
    _MJ_OK = True
    print(f"[mujoco] {_mj_pre.__version__} pre-loaded")
except Exception as _mj_e:
    print(f"[mujoco] not available ({_mj_e})")

if _IS_WIN:
    try:
        import ctypes as _ctypes
        _hwnd = _ctypes.windll.kernel32.GetConsoleWindow()
        if _hwnd:
            _ctypes.windll.user32.ShowWindow(_hwnd, 0)  # SW_HIDE
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────────────────────
#  QApplication MUST be created before ANYTHING that touches Qt
#  (including pyqtgraph.opengl which creates a hidden GLViewWidget)
# ──────────────────────────────────────────────────────────────────────────────
import os, json, csv, struct, threading, math, subprocess, traceback, queue
import numpy as np
_SUBPROCESS_FLAGS: dict = {"creationflags": 0x08000000} if _IS_WIN else {}
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict
from collections import deque

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QSlider, QLineEdit, QGroupBox,
    QStatusBar, QToolBar, QFileDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QCheckBox, QTextEdit, QTextBrowser, QSplitter, QMessageBox, QSizePolicy,
    QSpinBox, QProgressBar, QScrollArea, QFrame, QStackedWidget, QComboBox,
    QTabBar
)
from PyQt5.QtCore  import (Qt, QThread, QTimer, pyqtSignal, QSize, QRect, QRectF,
                           QPropertyAnimation, QEasingCurve, QPoint, QEvent, QSettings, QObject)
from PyQt5.QtGui   import (QPainter, QColor, QPen, QBrush, QFont,
                           QPixmap, QPainterPath, QLinearGradient)

# ── PyQt5 HiDPI — MUST come before QApplication() ────────────────────────────
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps,   True)

_app = QApplication.instance() or QApplication(sys.argv)

# --------------------------------------------------------------------------
#  THEME (Dark / Light) -- resolved here, early, so pyqtgraph's global config
#  (set up just below) and every widget constructed later can use LIGHT_MODE.
# --------------------------------------------------------------------------
def _load_light_mode_pref() -> bool:

    try:
        qs = QSettings("InspireHandController", "App")
        val = qs.value("ui/light_mode", False)
        return val in (True, "true", "1", 1)
    except Exception:
        return False

LIGHT_MODE = _load_light_mode_pref()

_DARK_PAL  = dict(win="#0d1117", panel="#161b22", input="#21262d",
                   border="#30363d", text="#d0d8e4", dim="#8b949e",
                   tab_sel_text="#ffffff", code_bg="#0d1117", code_text="#c9d1d9",
                   btn_neutral="#21262d")

_LIGHT_PAL = dict(win="#dee2e6", panel="#ced4da", input="#e9ecef",
                   border="#adb5bd", text="#343a40", dim="#6c757d",
                   tab_sel_text="#343a40", code_bg="#ced4da", code_text="#343a40",
                   btn_neutral="#495057")
PAL = _LIGHT_PAL if LIGHT_MODE else _DARK_PAL

BTN_TEXT = "#f3f5f7"

# ── Resolution preferences (Settings tab → Performance) ────────────────
# Read once at startup, same as the theme. Both are capped by the
# <global offwidth/offheight> ceiling declared in the hand XML files
# (2560x1920), so any preset below that works without touching the XML.
def _load_res_pref(key: str, default: int) -> int:
    try:
        qs = QSettings("InspireHandController", "App")
        return int(qs.value(f"perf/{key}", default))
    except Exception:
        return default

# Default 640x480: the embedded viewers are typically displayed at roughly
# half that, so rendering larger mostly pays for pixels that are discarded on
# downscale. Measured on the dev machine, 1280x960 costs ~3x the frame time of
# 640x480 for no visible gain at typical panel sizes. Raise it from Settings ->
# Performance if the viewer is enlarged or the machine has headroom.
MJ_RENDER_W  = _load_res_pref("render_w", 640)
MJ_RENDER_H  = _load_res_pref("render_h", 480)
CAM_CAP_W    = _load_res_pref("cam_w", 640)
CAM_CAP_H    = _load_res_pref("cam_h", 480)

RENDER_PRESETS = [("640 x 480 (default, fastest)", 640, 480),
                   ("960 x 720", 960, 720),
                   ("1280 x 960", 1280, 960),
                   ("1600 x 1200", 1600, 1200),
                   ("1920 x 1440 (sharpest, slowest)", 1920, 1440)]
CAM_PRESETS    = [("640 x 480 (fastest)", 640, 480),
                   ("960 x 540", 960, 540),
                   ("1280 x 720 (default HD)", 1280, 720),
                   ("1920 x 1080 (sharpest)", 1920, 1080)]


PG_OK = False
try:
    import pyqtgraph as pg
    pg.setConfigOption('background', PAL['win'])
    pg.setConfigOption('foreground', PAL['text'])
    PG_OK = True
except Exception: pass

GL_OK = False
try:
    import pyqtgraph.opengl as gl
    GL_OK = True
except Exception as _e:
    print(f"[3D] {_e}")

MB_OK = True   # raw socket always available — no pymodbus needed
HAND_PORT = 6000   # defined here for HandCom; also redefined in CONSTANTS block below

import socket as _socket

class HandCom:
    """
    Raw Modbus TCP communicator for Inspire Hand RH56DFTP.
    Uses raw socket like the proven reference code — unit_id=0xFF required.
    No pymodbus dependency.
    Angle convention matches hardware: 1000 = Open/Extended, 0 = Closed/Bent.
    """
    UNIT_ID = 0xFF   # Inspire Hand requires 0xFF — DO NOT change

    def __init__(self):
        self._sock = None
        self._lk   = threading.Lock()
        self._ok   = False
        self._tid  = 0

    # ── connection ────────────────────────────────────────────────────────
    def connect(self, ip:str, port:int=HAND_PORT, dev:int=None) -> bool:
        # dev param ignored — Inspire Hand always uses 0xFF
        try:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
            s.connect((ip, port))
            self._sock = s
            self._ok   = True
            self._w1(REG['CLEAR_ERR'], 1)
        except Exception as e:
            print(f"[COM] {e}"); self._ok = False
        return self._ok

    def disconnect(self):
        self._ok = False
        if self._sock:
            try: self._sock.close()
            except: pass
        self._sock = None

    @property
    def ok(self): return self._ok

    # ── low-level Modbus TCP ──────────────────────────────────────────────
    def _tid_next(self):
        self._tid = (self._tid + 1) & 0xFFFF
        return self._tid

    def _read(self, addr:int, n:int) -> Optional[List[int]]:
        """FC3 Read Holding Registers → list of n unsigned 16-bit values."""
        if not self._ok or not self._sock: return None
        with self._lk:
            try:
                tid = self._tid_next()
                req = struct.pack('>HHHBBHH',
                    tid, 0, 6, self.UNIT_ID, 3, addr, n)
                self._sock.sendall(req)
                resp = self._sock.recv(512)
                if len(resp) < 9 + n*2: return None
                return list(struct.unpack(f'>{n}H', resp[9:9+n*2]))
            except Exception as e:
                print(f"[COM read] {e}"); self._ok = False; return None

    def _write(self, addr:int, values:List[int]):
        if not self._ok or not self._sock or not values: return
        with self._lk:
            try:
                n   = len(values)
                pay = struct.pack(f'>{n}h', *values)
                bc  = n * 2
                tid = self._tid_next()
                # Length = UnitID(1)+FC(1)+Addr(2)+Count(2)+BC(1)+Data
                req = struct.pack('>HHHBBHHB',
                    tid, 0, bc+7, self.UNIT_ID, 16, addr, n, bc) + pay
                self._sock.sendall(req)
                self._sock.recv(64)   
            except Exception as e:
                print(f"[COM write] {e}"); self._ok = False

    def _w1(self, addr:int, v:int):
        """Write single register."""
        self._write(addr, [v & 0x7FFF])

    def _rs(self, addr, n):
        raw = self._read(addr, n)
        if raw is None: return None
        pk = struct.pack(f'>{n}H', *raw)
        return list(struct.unpack(f'>{n}h', pk))

    def _rb(self, addr, n):
        raw = self._read(addr, (n+1)//2)
        if raw is None: return None
        out = []
        for r in raw: out += [(r>>8)&0xFF, r&0xFF]
        return out[:n]

    def _rt(self, addr, blen, shape):
        """Read one tactile region. blen is the BYTE span of the region in the
        device address map; each sensing point is one 16-bit register, so the
        point count is blen//2 and must equal shape[0]*shape[1]."""
        raw = self._read(addr, blen//2)
        if raw is None: return None
        vals = [(r&0xFF)|((r>>8)<<8) for r in raw]
        return np.array(vals, np.uint16).reshape(shape)

    # ── high-level ────────────────────────────────────────────────────────
    def state(self) -> Optional[Dict]:
        a = self._rs(REG['ANGLE_ACT'], 6)
        if a is None: return None
        return dict(angle   = a,
                    force   = self._rs(REG['FORCE_ACT'], 6) or [0]*6,
                    pos     = self._rs(REG['POS_ACT'],   6) or [0]*6,
                    current = self._rs(REG['CURRENT'],   6) or [0]*6,
                    error   = self._rb(REG['ERROR'],  6)    or [0]*6,
                    status  = self._rb(REG['STATUS'], 6)    or [0]*6,
                    temp    = self._rb(REG['TEMP'],   6)    or [0]*6)

    def tactile(self) -> Dict:
        """Read all tactile sensors using merged, contiguous Modbus reads
        (≤125 registers each) instead of one round-trip per sensor pad —
        cuts 17 sequential reads down to ~11 for much lower latency."""
        out: Dict = {}
        for addr, n_regs, members in TACTILE_CHUNKS:
            raw = self._read(addr, n_regs)
            if raw is None:
                for key, shape, nr in members: out[key] = None
                continue
            off = 0
            for key, shape, nr in members:
                seg  = raw[off:off+nr]
                vals = [(r & 0xFF) | ((r >> 8) << 8) for r in seg]
                out[key] = np.array(vals, np.uint16).reshape(shape)
                off += nr
        return out

    # Angles: 1000=Open, 0=Closed  (hardware convention)
    def set_angles(self, v): self._write(REG['ANGLE_SET'], list(v))
    def set_forces(self, v): self._write(REG['FORCE_SET'], list(v))
    def set_speeds(self, v): self._write(REG['SPEED_SET'], list(v))
    def set_dspeed(self, v): self._write(REG['DEF_SPEED'], list(v))
    def set_dforce(self, v): self._write(REG['DEF_FORCE'], list(v))
    def calib(self):  self._w1(REG['FORCE_CLB'], 1)
    def clrerr(self): self._w1(REG['CLEAR_ERR'], 1)
    def save(self):   self._w1(REG['SAVE'],       1)
    def reset(self):  self._w1(REG['RESET'],      1)


TM_OK = False
try:
    import trimesh
    TM_OK = True
except Exception: pass

# ──────────────────────────────────────────────────────────────────────────────
#  STYLE & FONT
# ──────────────────────────────────────────────────────────────────────────────
FS   = 8           # base pt
FS_S = FS - 1
FS_L = FS + 2
AFNT = "Segoe UI" if _IS_WIN else "Ubuntu"

APP_QSS = f"""
* {{ font-family:"{AFNT}"; font-size:{FS}pt; color:{PAL['text']}; }}
QMainWindow,QWidget {{ background:{PAL['win']}; }}
QTabWidget::pane {{ background:{PAL['panel']}; border:1px solid {PAL['border']}; }}
QTabBar::tab {{ background:{PAL['input']}; color:{PAL['dim']}; padding:4px 19px;
                border:1px solid {PAL['border']}; border-bottom:none;
                border-radius:3px 3px 0 0; font-size:{FS}pt; }}
QTabBar::tab:selected {{ background:{PAL['panel']}; color:{PAL['tab_sel_text']}; border-top: 2px solid #257a04; }}
QGroupBox {{ background:{PAL['panel']}; border:1px solid {PAL['border']}; border-radius:5px;
             margin-top:1em; font-weight:bold; color:{PAL['dim']}; font-size:{FS}pt; }}
QGroupBox::title {{ subcontrol-origin:margin; left:8px; padding:0 4px; }}
QLabel    {{ color:{PAL['text']}; font-size:{FS}pt; background:transparent; }}
QLineEdit {{ background:{PAL['input']}; color:{PAL['text']}; border:1px solid {PAL['border']};
             border-radius:4px; padding:3px 6px; font-size:{FS}pt; }}
QSpinBox  {{ background:{PAL['input']}; color:{PAL['text']}; border:1px solid {PAL['border']};
             border-radius:4px; padding:2px; font-size:{FS}pt; }}
QComboBox {{ background:{PAL['input']}; color:{PAL['text']}; border:1px solid {PAL['border']};
             border-radius:4px; padding:2px 6px; font-size:{FS}pt; }}
QCheckBox {{ color:{PAL['text']}; font-size:{FS}pt; spacing:5px; background:transparent; }}
QCheckBox::indicator {{ width:14px; height:14px; }}
QSlider   {{ background:transparent; }}
QTextEdit {{ background:{PAL['code_bg']}; color:{PAL['code_text']};
             font-family:Consolas,monospace; font-size:{FS_S}pt;
             border:1px solid {PAL['border']}; border-radius:4px; }}
QScrollBar:vertical {{ background:{PAL['input']}; width:10px; border-radius:5px; }}
QScrollBar::handle:vertical {{ background:{PAL['border']}; border-radius:5px; min-height:24px; }}
QStatusBar {{ background:{PAL['panel']}; color:{PAL['dim']}; font-size:{FS_S}pt; }}
QToolBar   {{ background:{PAL['panel']}; border:none; spacing:4px; padding:3px; }}
QSplitter::handle {{ background:{PAL['border']}; border-radius:2px; }}
QSplitter::handle:horizontal {{ width:5px; }}
QSplitter::handle:vertical   {{ height:5px; }}
QTableWidget {{ background:{PAL['panel']}; color:{PAL['text']};
                gridline-color:{PAL['border']}; border:1px solid {PAL['border']};
                font-size:{FS_S}pt; }}
QTableWidget QTableCornerButton::section {{ background:{PAL['input']}; border:none; }}
QHeaderView::section {{ background:{PAL['input']}; color:{PAL['dim']}; border:none;
                        padding:3px; font-size:{FS_S}pt; font-weight:bold; }}
QProgressBar {{ background:{PAL['input']}; border-radius:4px; border:none; }}
QToolTip {{ 
    background-color: {PAL['panel']}; 
    color: {PAL['code_text']}; 
    border: 1px solid {PAL['border']}; 
    border-radius: 4px; 
    padding: 4px; 
}}
"""

def _mk_btn(label:str, bg:str="#21262d", bold:bool=False) -> QPushButton:
    b = QPushButton(label)
    fw = "bold" if bold else "normal"
    def _lh(h): c=QColor(h); return QColor(min(255,c.red()+22),min(255,c.green()+22),min(255,c.blue()+22)).name()
    b.setStyleSheet(
        f"QPushButton{{background:{bg};color:{BTN_TEXT};"
        f"border:1px solid {PAL['border']};border-radius:4px;"
        f"padding:4px 10px;font-size:{FS}pt;font-weight:{fw};}}"
        f"QPushButton:hover{{background:{_lh(bg)};border-color:#58a6ff;}}"
        f"QPushButton:pressed{{padding:5px 9px 3px 11px;}}")
    return b

# ──────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────
APP_VER   = "4.0.0"

# ── Project metadata (shown in the About tab; also mirrored in README/paper) ──
APP_NAME        = "Dexterous Hand Controller"
APP_DESCRIPTION = (
    "A dual-hand PyQt5 control, calibration, and teleoperation suite for the "
    "Inspire Hand RH56 series anthropomorphic robotic hand. Provides real-time "
    "Modbus-TCP device control, MuJoCo-based 3D kinematic visualization with "
    "tactile-sensor overlays, webcam-driven motion-capture teleoperation, and "
    "trainable gesture recognition, for one or two hands simultaneously."
)
APP_AUTHORS = [
    {"name": "Hamid Reza Saadati",
     "affiliation": "University of Duisburg-Essen; Aalen University",
     "orcid": "0009-0004-7550-3400"},
    {"name": "Wojciech Kowalczyk",
     "affiliation": "University of Duisburg-Essen",
     "orcid": "0000-0002-7390-2836"},
    {"name": "Sebastian Feldmann",
     "affiliation": "Aalen University",
     "orcid": "0000-0001-6054-7292"},
]
APP_LICENSE     = "GPL-3.0-or-later"
APP_REPO_URL    = "https://github.com/HamidRobotics/inspire-hand-controller"
APP_CITE_NOTE   = "Citation details will be added here once the accompanying paper is published."

POLL_MS   = 20        # poller sleep – not the UI refresh rate
UI_HZ_MS  = 16        # UI refresh timer (~62 Hz). Must stay faster than
                      # POLL_MS: the state buffer holds only the newest
                      # sample, so a slower UI would drop samples unevenly.
HIST_LEN  = 350

DOF_NAMES  = ['Pinky','Ring','Middle','Index','Thumb Bend','Thumb Rot']
DOF_COLORS = ['#FF6B6B','#FFB347','#FFD700','#98FB98','#87CEEB','#DDA0DD']

REG = dict(
    HAND_ID=1000,BAUD=1002,CLEAR_ERR=1004,SAVE=1005,RESET=1006,
    FORCE_CLB=1009,DEF_SPEED=1032,DEF_FORCE=1044,
    POS_SET=1474,ANGLE_SET=1486,FORCE_SET=1498,SPEED_SET=1522,
    POS_ACT=1534,ANGLE_ACT=1546,FORCE_ACT=1582,
    CURRENT=1594,ERROR=1606,STATUS=1612,TEMP=1618,
)

TACTILE_DEFS = [
    ("Pinky Tip",  3000, 18,  (3, 3), "pinky_tip"),
    ("Pinky Nail", 3018, 192,(12, 8), "pinky_nail"),
    ("Pinky Pad",  3210, 160,(10, 8), "pinky_pad"),
    ("Ring Tip",   3370, 18,  (3, 3), "ring_tip"),
    ("Ring Nail",  3388, 192,(12, 8), "ring_nail"),
    ("Ring Pad",   3580, 160,(10, 8), "ring_pad"),
    ("Middle Tip", 3740, 18,  (3, 3), "middle_tip"),
    ("Middle Nail",3758, 192,(12, 8), "middle_nail"),
    ("Middle Pad", 3950, 160,(10, 8), "middle_pad"),
    ("Index Tip",  4110, 18,  (3, 3), "index_tip"),
    ("Index Nail", 4128, 192,(12, 8), "index_nail"),
    ("Index Pad",  4320, 160,(10, 8), "index_pad"),
    ("Thumb Tip",  4480, 18,  (3, 3), "thumb_tip"),
    ("Thumb Nail", 4498, 192,(12, 8), "thumb_nail"),
    ("Thumb Mid",  4690, 18,  (3, 3), "thumb_mid"),
    ("Thumb Pad",  4708, 192,(12, 8), "thumb_pad"),
    ("Palm",       4900, 224,(14, 8), "palm"),
]

def _build_tactile_chunks(defs, max_regs=125):
    """
    Group the (perfectly-contiguous) tactile register ranges into the fewest
    possible Modbus reads, capped at 125 registers (the FC3 spec limit).
    Cuts tactile() from 17 sequential round-trips down to ~11, which —
    combined with TCP_NODELAY — removes most of the read latency.
    """
    chunks = []
    cur_addr = None; cur_regs = 0; cur_members = []
    for _, addr, blen, shape, key in defs:
        n_regs = blen // 2
        expected_addr = (cur_addr + cur_regs) if cur_addr is not None else addr
        if cur_members and (cur_regs + n_regs > max_regs or addr != expected_addr):
            chunks.append((cur_addr, cur_regs, cur_members))
            cur_members = []; cur_regs = 0; cur_addr = None
        if cur_addr is None: cur_addr = addr
        cur_members.append((key, shape, n_regs))
        cur_regs += n_regs
    if cur_members:
        chunks.append((cur_addr, cur_regs, cur_members))
    return chunks

TACTILE_CHUNKS = _build_tactile_chunks(TACTILE_DEFS)

ERR_BITS = {0:"Stall",1:"OvrTemp",2:"OvrCurr",3:"MotErr",4:"CommErr"}
STATUS_T = {0:"Releasing",1:"Grasping",2:"Pos-Stop",3:"Force-Stop",
            5:"Curr-Prot",6:"Stall",7:"Fault",255:"Error"}

HAND_L_IP = "192.168.123.210"
HAND_R_IP = "192.168.123.211"
HAND_PORT = 6000

# ──────────────────────────────────────────────────────────────────────────────
#  COLOUR HELPERS
# ──────────────────────────────────────────────────────────────────────────────
def touch_qc(val:int, mx:int=4095) -> QColor:
    """Blue=no press → cyan → green → yellow → Red=hard press."""
    v = max(0., min(1., val / max(mx,1)))
    if v < 0.02: return QColor(PAL['input'])
    if v < 0.25:
        t=v/0.25; return QColor(0,int(t*80),255)
    if v < 0.5:
        t=(v-0.25)/0.25; return QColor(0,int(80+t*175),int(255*(1-t)))
    if v < 0.75:
        t=(v-0.5)/0.25; return QColor(int(t*255),255,0)
    t=(v-0.75)/0.25; return QColor(255,int(255*(1-t)),0)

def touch_rgba(val:int, mx:int=4095) -> tuple:
    v=max(0.,min(1.,val/max(mx,1))); c=touch_qc(val,mx)
    return (c.redF(),c.greenF(),c.blueF(), 0.0 if v<0.02 else min(1.,v*1.5+0.2))

# ──────────────────────────────────────────────────────────────────────────────
#  THREAD-SAFE STATE BUFFER  ← key fix for dual-hand stability
# ──────────────────────────────────────────────────────────────────────────────
class StateBuffer:
    """Pollers write here; the UI timer reads on the main thread."""
    def __init__(self):
        self._lk   = threading.Lock()
        self._st   = None
        self._td   = None
        self._st_n = False
        self._td_n = False

    def put_state(self, s:Dict):
        with self._lk: self._st=s; self._st_n=True

    def put_tac(self, td:Dict):
        with self._lk: self._td=td; self._td_n=True

    def take(self):
        """Returns (state|None, tactile|None) and clears 'new' flags."""
        with self._lk:
            s  = self._st if self._st_n else None
            td = self._td if self._td_n else None
            self._st_n=False; self._td_n=False
        return s, td

    def peek_state(self):
        """Like take(), but does NOT clear the 'new' flag or affect the
        regular UI polling loop — for code that needs to read the latest
        known state without consuming it (e.g. HWSequencer)."""
        with self._lk:
            return self._st


class HWSequencer(QObject):

    STEP_HW     = 15    # hw-units moved per tick toward target
    INTERVAL_MS = 35
    MAX_TICKS   = 140   # ~4.9s hard safety cap — see send()/​_tick() below

    def __init__(self, sess, parent=None):
        super().__init__(parent)
        self.sess = sess
        self._target = None
        self._staged = None
        self._ticks  = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def send(self, target: list):
        if self.sess.demo_mode:
            return
            
        self._ticks = 0  # Reset safety limit so continuous motion capture doesn't time out
        
        real = self.sess.buf.peek_state()
        if self._staged is None:
            self._staged = list(real['angle']) if real and 'angle' in real else list(target)
            
        self._target = list(target)
        
        if not self._timer.isActive():
            self._timer.start(self.INTERVAL_MS)

    def stop(self):
        self._timer.stop(); self._target = None; self._staged = None

    def _tick(self):
        if self._target is None or self.sess.demo_mode:
            self._timer.stop(); return
            
        self._ticks += 1
        if self._ticks > self.MAX_TICKS:
            self._timer.stop(); self._target = None; return

        # Use our internal staged state as the base so network lag doesn't cause stuttering
        cur = list(self._staged) 

        DB = SimTuning.seq_deadband
        DONE_TOL = 20
        hold_thumb = [False, False]
        hold_finger = [False]*4
        
        # Keep thumb/finger gating ONLY if enabled in Settings
        if SimTuning.hwseq_enabled:
            if self._target[4] < cur[4] - DB:
                if abs(self._target[3] - cur[3]) > DONE_TOL:
                    hold_thumb = [True, True]
            elif self._target[4] > cur[4] + DB:
                if abs(self._target[4] - cur[4]) > DONE_TOL:
                    hold_finger = [True]*4

        new_staged = list(cur)
        all_reached = True
        
        # Grab the smoothing value from your Settings slider
        alpha = SimTuning.smooth_alpha

        for dof in range(6):
            if dof >= 4 and hold_thumb[dof-4]:
                new_staged[dof] = cur[dof]; all_reached = False; continue
            if dof < 4 and hold_finger[dof]:
                new_staged[dof] = cur[dof]; all_reached = False; continue
                
            diff = self._target[dof] - cur[dof]
            
            # Snap to target if we are extremely close
            if abs(diff) <= 2.0:
                new_staged[dof] = self._target[dof]
            else:
                # Proportional smoothing step
                step = diff * alpha
                
                # Ensure a minimum speed of 1.0 unit per tick so it doesn't freeze near the end
                if step > 0:
                    step = max(1.0, step)
                else:
                    step = min(-1.0, step)
                    
                new_staged[dof] = cur[dof] + step
                all_reached = False
                
        self._staged = new_staged
        try:
            self.sess.com.set_angles([int(max(0, min(1000, x))) for x in new_staged])
        except Exception as e:
            print(f"[HWSequencer:{self.sess.name}] {e}")
            
        if all_reached:
            self._timer.stop(); self._target = None

# ──────────────────────────────────────────────────────────────────────────────
#  HAND COMMUNICATOR
# ──────────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────────
#  DEMO GENERATOR
# ──────────────────────────────────────────────────────────────────────────────
class DemoGen:
    # ---- Demo-motion tuning (no hardware; purely for the synthetic preview) ----
    # DOF order is DOF_NAMES: 0 Pinky, 1 Ring, 2 Middle, 3 Index,
    # 4 Thumb bend, 5 Thumb rot.
    #
    # Each channel is  centre + amplitude * sin(t + phase).
    # The thumb is deliberately kept out of phase with the index and given a
    # smaller travel about a more open centre, so the two do not swing through
    # each other in the simulated view. Raise DEMO_PHASE[4] toward
    # DEMO_PHASE[3] to make them meet sooner, lower DEMO_AMP[4] or raise
    # DEMO_CENTRE[4] to keep the thumb further clear of the index path.
    DEMO_PHASE  = [0.0, 0.9, 1.8, 2.7, 5.85, 5.85]   # radians
    DEMO_AMP    = [450, 450, 450, 450,  260,  450]   # hw units
    DEMO_CENTRE = [500, 500, 500, 500,  660,  620]   # hw units
    # Wall-clock rate. 0.12 rad per tick at the nominal 50 Hz poll rate
    # is 6.0 rad/s; change DEMO_RATE_HZ to speed the demo up or slow it down.
    DEMO_RATE_HZ = 6.0                                # radians per second

    def __init__(self, phase=0.):
        self._phase = phase
        self._t0 = time.perf_counter()
        self._t = phase

    def tick(self):
        # Advance on the wall clock, not once per call. The poller is a Python
        # thread competing for the GIL with rendering, so its real rate sags
        # from 50 Hz to ~19 Hz under load. Stepping a fixed amount per call
        # made the demo waveform slow down and turn lumpy exactly when the
        # simulation was busiest; deriving the phase from elapsed time keeps a
        # clean constant-rate sinusoid however irregularly it is sampled.
        self._t = self._phase + (time.perf_counter() - self._t0) * self.DEMO_RATE_HZ
    def state(self)->Dict:
        self.tick()
        a=[max(0, min(1000, int(self.DEMO_CENTRE[i]
                                + self.DEMO_AMP[i]*math.sin(self._t+self.DEMO_PHASE[i]))))
           for i in range(6)]

        f=[int(350*(1.0+math.sin(self._t*1.3+i)))  for i in range(6)]
        c=[int(225*(1.0+math.sin(self._t*0.8+i)))  for i in range(6)]
        t=[int(37+9*math.sin(self._t*0.12+i))     for i in range(6)]
        return dict(angle=a,force=f,pos=[v*2 for v in a],
                    current=c,error=[0]*6,status=[2]*6,temp=t)
    def tactile(self)->Dict:
        """Synthetic tactile field. Real sensors read ~0 except where the
        hand actually touches something, so only a small travelling patch
        is driven here; a uniformly-lit field would be both unrealistic and
        far more expensive to draw."""
        out={}
        for _,_2,_3,sh,key in TACTILE_DEFS:
            rows,cols=sh
            rr=np.arange(rows).reshape(-1,1)
            cc=np.arange(cols).reshape(1,-1)
            # contact patch drifts across the pad as the demo cycles
            cr=(0.5+0.5*math.sin(self._t*0.45))*(rows-1)
            cc0=(0.5+0.5*math.cos(self._t*0.31))*(cols-1)
            d2=((rr-cr)**2+(cc-cc0)**2)
            amp=700+1800*abs(math.sin(self._t*0.45))
            field=amp*np.exp(-d2/2.0)
            out[key]=field.astype(np.uint16)
        return out

# ──────────────────────────────────────────────────────────────────────────────
#  HAND SESSION
# ──────────────────────────────────────────────────────────────────────────────
class HandSession:
    def __init__(self,name,default_ip,accent,demo_phase=0.):
        self.name      = name
        self.ip        = default_ip
        self.accent    = accent
        self.com       = HandCom()
        self.demo      = DemoGen(demo_phase)
        self.buf       = StateBuffer()
        self.poller: Optional['DataPoller'] = None
        self.demo_mode = True
        self.connected = False

# ──────────────────────────────────────────────────────────────────────────────
#  DATA POLLER  ← writes to StateBuffer only; never touches Qt/UI
# ──────────────────────────────────────────────────────────────────────────────
class DataPoller(QThread):
    lost_conn = pyqtSignal()

    def __init__(self,sess:HandSession):
        super().__init__()
        self.sess      = sess
        self._run      = True
        self._tac      = True
        self._tc       = 0
        self._interval = POLL_MS
        self.setObjectName(f"Poller-{sess.name}")

    def set_tac(self,v): self._tac=v
    def set_interval(self,ms:int): self._interval=max(10,int(ms))
    def stop(self):      self._run=False

    def run(self):
        self._next = time.monotonic()
        while self._run:
            try:
                if self.sess.demo_mode:
                    st = self.sess.demo.state()
                    # Stamp the sample where it is produced, so the plot can
                    # place it at its true time rather than at the moment the
                    # GUI collected it. perf_counter, not monotonic: monotonic
                    # ticks every 15.6 ms on Windows, coarser than the sample
                    # interval.
                    st['_t'] = time.perf_counter()
                    self.sess.buf.put_state(st)
                    if self._tac:
                        self.sess.buf.put_tac(self.sess.demo.tactile())
                else:
                    st=self.sess.com.state()
                    if st is None:
                        self.lost_conn.emit(); return
                    st['_t'] = time.perf_counter()
                    self.sess.buf.put_state(st)
                    self._tc+=1
                    if self._tac:
                        self.sess.buf.put_tac(self.sess.com.tactile())
            except Exception as e:
                print(f"[Poller:{self.sess.name}] {e}")
            # Sleep to the next period boundary, not a fixed amount after the
            # work: a flat msleep(interval) would make the true period
            # work+interval and drift with load.
            self._next += self._interval / 1000.0
            slack = self._next - time.monotonic()
            if slack > 0:
                self.msleep(int(slack * 1000))
            else:
                self._next = time.monotonic()   # fell behind: resync, don't spiral

# ──────────────────────────────────────────────────────────────────────────────
#  3-D HELPERS
# ──────────────────────────────────────────────────────────────────────────────
def _rod(v,axis,angle):
    c,s=math.cos(angle),math.sin(angle)
    return v*c+np.cross(axis,v)*s+axis*np.dot(axis,v)*(1-c)

def _xform(pts,origin,direction):
    z=direction/(np.linalg.norm(direction)+1e-9)
    ref=np.array([0.,1.,0.]) if abs(z[1])<0.9 else np.array([1.,0.,0.])
    x=np.cross(ref,z); x/=(np.linalg.norm(x)+1e-9)
    y=np.cross(z,x)
    return pts @ np.column_stack([x,y,z]).T + origin

def _cylinder(p0,p1,r,n=14,clr=(0.2,0.35,0.6,1.)):
    vec=p1-p0; h=float(np.linalg.norm(vec))
    if h<0.5:
        return dict(vertexes=np.zeros((3,3)),faces=np.array([[0,1,2]]),
                    faceColors=np.ones((1,4)),smooth=False,drawFaces=True)
    verts,faces=[],[]
    for k in range(n):
        a=2*math.pi*k/n
        verts+=[[r*math.cos(a),r*math.sin(a),0.],[r*math.cos(a),r*math.sin(a),h]]
    verts+=[[0.,0.,0.],[0.,0.,h]]
    for k in range(n):
        j=(k+1)%n; b0,b1,t0,t1=2*k,2*j,2*k+1,2*j+1
        faces+=[[b0,b1,t1],[b0,t1,t0],[2*n,b1,b0],[2*n+1,t0,t1]]
    v=_xform(np.array(verts,float),p0,vec/h)
    f=np.array(faces,int)
    return dict(vertexes=v,faces=f,faceColors=np.tile(clr,(len(f),1)),
                smooth=True,drawFaces=True,drawEdges=False)

class HandSketch2D(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.a=[0]*6; self.f=[0]*6
        self.setMinimumSize(160,220); self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
    def update_state(self,s):
        self.a=s.get('angle',[0]*6); self.f=s.get('force',[0]*6); self.update()
    def paintEvent(self,_):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W,H=self.width(),self.height()
        p.fillRect(0,0,W,H,QColor(PAL['win']))
        sc=min(W/210,H/285); p.translate(W/2,H/2); p.scale(sc,sc); p.translate(-95,-128)
        self._draw(p); p.end()
    def _draw(self,p):
        p.setPen(QPen(QColor(55,70,95),2)); p.setBrush(QBrush(QColor(35,45,62)))
        p.drawRoundedRect(QRect(20,126,138,94),12,12)
        conf=[(43,20,88,0),(77,22,96,1),(111,24,102,2),(145,22,94,3)]
        for fx,fw,mxL,di in conf:
            af=max(0,min(1000,self.a[di]))/1000.; ext=mxL*(1.-af*0.73); top=126-ext
            ff=min(1.,abs(self.f[di])/3000.); qc=QColor(DOF_COLORS[di])
            r2=QColor(int(qc.red()*(1-ff)+240*ff),int(qc.green()*(1-ff)),int(qc.blue()*(1-ff)),200)
            p.setBrush(QBrush(r2)); p.setPen(QPen(r2.lighter(130),1))
            p.drawRoundedRect(QRect(fx,int(top),fw,int(126-top+7)),7,7)
            p.setBrush(QBrush(r2.lighter(155))); p.setPen(Qt.NoPen)
            p.drawEllipse(QRect(fx,int(top),fw,13))
        tb=max(0,min(1000,self.a[4]))/1000.; tr=max(0,min(1000,self.a[5]))/1000.
        tL=53+(1-tb)*28; ta=54+tr*45
        p.save(); p.translate(20,180); p.rotate(-ta)
        qct=QColor(DOF_COLORS[4]); qct.setAlpha(200)
        p.setBrush(QBrush(qct)); p.setPen(QPen(qct.lighter(130),1))
        p.drawRoundedRect(QRect(-10,-int(tL),20,int(tL)+6),7,7); p.restore()
        p.setPen(QColor(110,125,150)); p.setFont(QFont(AFNT,7))
        for lbl,fx in zip(['Pi','Ri','Mi','In'],[43,77,111,145]): p.drawText(fx,240,lbl)
        p.drawText(14,240,'Th')

# ──────────────────────────────────────────────────────────────────────────────
#  TACTILE 2-D WIDGETS
# ──────────────────────────────────────────────────────────────────────────────
class FingerTacWidget(QWidget):
    def __init__(self,name,keys,parent=None):
        super().__init__(parent)
        self.name=name; self.keys=keys; self.data={}
        self.setMinimumWidth(50); self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
    def update_data(self,td):
        for k in self.keys: self.data[k]=td.get(k)
        self.update()
    def paintEvent(self,_):
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        W,H=self.width(),self.height()
        p.fillRect(0,0,W,H,QColor(PAL['win']))
        shapes=[]
        for k in self.keys:
            for _,_2,_3,sh,kk in TACTILE_DEFS:
                if kk==k: shapes.append((k,sh)); break
        if not shapes: return
        tot=sum(s[0] for _,s in shapes)+3*(len(shapes)-1)
        mc=max(s[1] for _,s in shapes)
        ch=(H-22)/max(tot,1); cw=(W-4)/max(mc,1)
        y=20.
        # Use small font for numbers inside cells
        nfont=QFont(AFNT, max(5, min(9, int(min(cw,ch)*0.55))))
        for k,(r,c) in shapes:
            mat=self.data.get(k); x0=(W-c*cw)/2
            for ri in range(r):
                for ci in range(c):
                    val=int(mat[ri,ci]) if mat is not None else 0
                    rx=int(x0+ci*cw)+1; ry=int(y+ri*ch)+1
                    rw=max(1,int(cw)-1); rh=max(1,int(ch)-1)
                    p.fillRect(rx,ry,rw,rh,touch_qc(val))
                    # Show value number inside cell if large enough
                    if rw>=18 and rh>=10 and val>0:
                        p.setFont(nfont)
                        tc=QColor(0,0,0) if val>2000 else QColor(220,230,245)
                        p.setPen(tc)
                        p.drawText(QRectF(rx,ry,rw,rh),Qt.AlignCenter,str(val))
            y+=r*ch+3*ch
        p.setPen(QColor(PAL['dim'])); p.setFont(QFont(AFNT,FS_S-1,QFont.Bold))
        p.drawText(3,15,self.name); p.end()

class PalmTacWidget(QWidget):
    """Palm tactile: 8 rows × 23 cols (matches real hardware matrix)."""
    ROWS=8; COLS=14
    def __init__(self,parent=None):
        super().__init__(parent)
        self.mat=None; self.setMinimumSize(120,80)
        self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
    def update_data(self,td): self.mat=td.get('palm'); self.update()

    def paintEvent(self,_):
        p=QPainter(self); W,H=self.width(),self.height()
        p.fillRect(0,0,W,H,QColor(PAL['win']))
        
        rows,cols=self.ROWS,self.COLS
        ch=(H-18)/rows; cw=(W-4)/cols
        nfont=QFont(AFNT, max(5, min(9, int(min(cw,ch)*0.55))))

        for r in range(rows):
            for c in range(cols):
                ir=r   # flip so wrist is at bottom
                val=0
                
                if self.mat is not None:
                    mr,mc=self.mat.shape  # Hardware sends (14, 8)
                    
                    # TRANSPOSE THE LOOKUP:
                    # We map visual column 'c' (0-13) to data row (mr).
                    # We map visual row 'ir' (0-7) to data col (mc).
                    if c < mr and ir < mc:
                        val=int(self.mat[c, ir])
                        
                rx=int(2+c*cw)+1; ry=int(18+r*ch)+1
                rw=max(1,int(cw)-1); rh=max(1,int(ch)-1)
                p.fillRect(rx,ry,rw,rh,touch_qc(val))
                if rw>=18 and rh>=10 and val>0:
                    p.setFont(nfont)
                    p.setPen(QColor(0,0,0) if val>2000 else QColor(220,230,245))
                    p.drawText(QRectF(rx,ry,rw,rh),Qt.AlignCenter,str(val))
                    
        p.setPen(QColor(PAL['dim'])); p.setFont(QFont(AFNT,FS_S-1,QFont.Bold))
        p.drawText(3,14,"Palm")
        p.end()

class ColorBarWidget(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent); self.setFixedWidth(10)
        self.setSizePolicy(QSizePolicy.Fixed,QSizePolicy.Expanding)
    def paintEvent(self,_):
        p=QPainter(self); H=self.height()
        for y in range(H):
            p.setPen(touch_qc(int((1-y/H)*4095))); p.drawLine(2,y,self.width()-1,y)
        p.setPen(QColor(PAL['dim'])); p.setFont(QFont(AFNT,10,QFont.Bold))
        p.drawText(3,10,"↑"); p.drawText(3,H-3,"↓")
        p.end()

# ──────────────────────────────────────────────────────────────────────────────
#  FINGER ICON  — loads hand images icons from finger_icons/ subfolder.
#  Scales uniformly (Qt.KeepAspectRatio) — width and height are NEVER set
#  independently, only a single scale factor is applied.
#  dof_idx: 0=Pinky 1=Ring 2=Middle 3=Index 4=ThumbBend 5=ThumbRot
#  hand_side: 'L' or 'R'
# ──────────────────────────────────────────────────────────────────────────────
class FingerIcon(QWidget):
    _NAMES = {0:'pinky', 1:'ring', 2:'middle', 3:'index',
               4:'thumb_bend', 5:'thumb_rot'}
    _GESTURE = {'point':'gesture_point', 'Victory':'gesture_Victory'}

    @classmethod
	
    def gesture(cls, key:str, size:int=40, side:str='L', parent=None) -> 'FingerIcon':
        w = cls.__new__(cls)
        QWidget.__init__(w, parent)
        w.dof   = -1   # not a DOF icon
        w.side  = side
        w._size = size
        w._gkey = key
        fname   = cls._GESTURE.get(key, key)
        path    = cls._ICON_DIR / f"{fname}_{side}.png"
        cache_k = (f'g:{key}', side)
        if cache_k not in cls._CACHE:
            cls._CACHE[cache_k] = QPixmap(str(path)) if path.exists() else None
        pm = cls._CACHE.get(cache_k)

        return w
        
    def _pm_for_paint(self):
        if hasattr(self, '_gkey'):
            key = (f'g:{self._gkey}', self.side)
            return self._CACHE.get(key)
        return self._get_pm(self.dof, self.side)
    _ICON_DIR = Path(__file__).parent / 'finger_icons'
    _CACHE: Dict = {}   

    def __init__(self, dof_idx:int, size:int=40, hand_side:str='L', parent=None):
        super().__init__(parent)
        self.dof   = dof_idx
        self.side  = hand_side
        self._size = size  
        pm = self._get_pm(dof_idx, hand_side)
        if pm and not pm.isNull():
            scaled = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.setFixedSize(scaled.size())
        else:
            self.setFixedSize(size, size)

    @classmethod
    def _get_pm(cls, dof:int, side:str) -> Optional[QPixmap]:
        key = (dof, side)
        if key not in cls._CACHE:
            base  = cls._NAMES.get(dof, 'pinky')
            fname = f"{base}_{side}.png"
            path  = cls._ICON_DIR / fname
            if path.exists():
                cls._CACHE[key] = QPixmap(str(path))
            else:
                cls._CACHE[key] = None
        return cls._CACHE[key]

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.SmoothPixmapTransform)
        W, H = self.width(), self.height()
        pm = self._pm_for_paint()
        if pm and not pm.isNull():
            scaled = pm.scaled(W, H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x = (W - scaled.width())  // 2
            y = (H - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)
        else:
            col = DOF_COLORS[self.dof] if 0 <= self.dof < len(DOF_COLORS) else '#ffffff'
            p.setPen(QColor(col))
            p.setFont(QFont(AFNT, max(6, self._size // 5)))
            lbl = getattr(self, '_gkey', DOF_NAMES[self.dof][:3] if self.dof >= 0 else '?')
            p.drawText(QRectF(0, 0, W, H), Qt.AlignCenter, str(lbl)[:3])
        p.end()

# ──────────────────────────────────────────────────────────────────────────────
#  STATUS TABLE
# ──────────────────────────────────────────────────────────────────────────────
class StatusTable(QTableWidget):
    _ROWS=['Angle','Force g','Curr mA','Temp °C','Error','Status','Pos']
    def __init__(self,parent=None):
        super().__init__(len(self._ROWS),6,parent)
        self.setVerticalHeaderLabels(self._ROWS)
        self.setHorizontalHeaderLabels(DOF_NAMES)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.verticalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionMode(QTableWidget.NoSelection)
        self.setMinimumHeight(175)
        for r in range(len(self._ROWS)):
            for c in range(6):
                it=QTableWidgetItem("—"); it.setTextAlignment(Qt.AlignCenter)
                self.setItem(r,c,it)
    def update_state(self,s):
        rows=[s.get('angle',[0]*6),s.get('force',[0]*6),s.get('current',[0]*6),
              s.get('temp',[0]*6),s.get('error',[0]*6),s.get('status',[0]*6),s.get('pos',[0]*6)]
        for ri,rd in enumerate(rows):
            for ci in range(6):
                v=rd[ci] if ci<len(rd) else 0
                if ri==4:
                    txt=','.join(ERR_BITS[b] for b in range(5) if v&(1<<b)) or "OK"
                    clr=QColor('#FF6B6B') if v else QColor('#3fb950')
                elif ri==5: txt=STATUS_T.get(v,str(v)); clr=QColor('#87CEEB')
                elif ri==3: txt=str(v); clr=QColor('#FF6B6B') if v>60 else QColor('#FFD700') if v>45 else QColor(PAL['text'])
                else: txt=str(v); clr=QColor(PAL['text'])
                it=self.item(ri,ci)
                if it: it.setText(txt); it.setForeground(clr)

# ──────────────────────────────────────────────────────────────────────────────
#  PLOT PANEL  — horizontal icon-legend above the graph, legend removed from plot
# ──────────────────────────────────────────────────────────────────────────────
class PlotPanel(QWidget):
    def __init__(self,title,ylabel,yr=None,hand_side='L',parent=None):
        super().__init__(parent)
        self._h=[deque([0.]*HIST_LEN,maxlen=HIST_LEN) for _ in range(6)]
        self._t0=None        # set from the first sample after reset()
        self._x=deque([0.]*HIST_LEN,maxlen=HIST_LEN)
        self._dirty=False
        self._redraw=None
        self._n=0            # real samples pushed so far (deques start pre-filled)
        lay=QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(1)

        # ── horizontal legend row with finger icons ───────────────────────
        leg_row=QHBoxLayout(); leg_row.setSpacing(6); leg_row.setContentsMargins(4,2,4,0)
        for i in range(6):
            cell=QHBoxLayout(); cell.setSpacing(2)
            icon=FingerIcon(i,28,hand_side); cell.addWidget(icon)
            lbl=QLabel(DOF_NAMES[i]); lbl.setStyleSheet(f"color:{DOF_COLORS[i]};font-size:{FS_S}pt;font-weight:bold;")
            cell.addWidget(lbl); leg_row.addLayout(cell)
        leg_row.addStretch()
        lay.addLayout(leg_row)

        if not PG_OK:
            lay.addWidget(QLabel(f"{title} — pip install pyqtgraph")); return
        self.pw=pg.PlotWidget(title=title)
        self.pw.setLabel('left',ylabel)
        self.pw.setLabel('bottom','time since connect (s)')
        self.pw.showGrid(x=True,y=True,alpha=0.14); self.pw.setBackground(PAL['win'])
        if yr: self.pw.setYRange(*yr)
        self._cv=[self.pw.plot(pen=pg.mkPen(DOF_COLORS[i],width=1.5)) for i in range(6)]
        lay.addWidget(self.pw,1)

    REDRAW_HZ = 25          # curve repaints per second

    def reset(self):
        """Clear history so the x axis restarts at 0 for a new connection."""
        for dq in self._h: dq.clear(); dq.extend([0.]*HIST_LEN)
        self._x.clear(); self._x.extend([0.]*HIST_LEN)
        self._t0 = None; self._n = 0; self._dirty = True

    def push(self, vs, t=None):
        """Record a sample. Every sample is kept; only the repaint is rated.

        Calling setData on six curves per plot at the full device rate is more
        than the interface can sustain, and dropping samples would deform the
        trace, so ingestion and redraw are decoupled."""
        for i,v in enumerate(vs[:6]): self._h[i].append(float(v))
        if t is None: t = time.perf_counter()
        if self._t0 is None: self._t0 = t
        self._x.append(t - self._t0)
        self._n += 1
        if not PG_OK: return
        self._dirty = True
        if self._redraw is None:
            self._redraw = QTimer(self)
            self._redraw.timeout.connect(self._flush)
            self._redraw.start(int(1000 / self.REDRAW_HZ))

    def _flush(self):
        if not self._dirty or not PG_OK: return
        self._dirty = False
        n = min(self._n, HIST_LEN)
        if n < 2: return
        try:
            xs = list(self._x)[-n:]
            for i,c in enumerate(self._cv):
                c.setData(xs, list(self._h[i])[-n:])
        except: pass

# ──────────────────────────────────────────────────────────────────────────────
#  SINGLE-HAND DASHBOARD PANEL
# ──────────────────────────────────────────────────────────────────────────────
class HandDashPanel(QWidget):
    def __init__(self,sess:HandSession,parent=None):
        super().__init__(parent)
        self.sess=sess
        root=QVBoxLayout(self); root.setContentsMargins(3,3,3,3); root.setSpacing(4)
        title=QLabel(f"{'◀' if sess.name=='Left' else '▶'}  {sess.name} Hand")
        title.setStyleSheet(f"color:{sess.accent};font-size:{FS_L}pt;font-weight:bold;padding:2px;")
        root.addWidget(title)

        # ── top splitter: MuJoCo sim  |  live data table ──────────────────
        top=QSplitter(Qt.Horizontal); top.setHandleWidth(7)
        _here=Path(__file__).parent
        side='right' if sess.name=='Right' else 'left'
        _def=next((str(p) for p in [_here/f'inspire_hand_{side}.xml',
                                     _here/f'rh56dftp_{side}.xml'] if p.exists()),'')
        mj_gb=QGroupBox("🤖 MuJoCo Simulation"); ml=QVBoxLayout(mj_gb)
        self.mj_widget=MuJoCoWidget(sess.name,_def); ml.addWidget(self.mj_widget)
        top.addWidget(mj_gb)

        tbl_gb=QGroupBox("📊 Live Data"); tl=QVBoxLayout(tbl_gb)
        self.tbl=StatusTable(); tl.addWidget(self.tbl)
        top.addWidget(tbl_gb)
        top.setSizes([340,380])

        # ── bottom splitter: angle/force/current plots  |  tactile ────────────
        bot=QSplitter(Qt.Vertical); bot.setHandleWidth(7)
        hs = 'R' if sess.name=='Right' else 'L'

        pt=QTabWidget()
        self.p_ang=PlotPanel("Angles","–0–1000",(0,1000),hand_side=hs)
        self.p_frc=PlotPanel("Forces","g",(-4000,4000),hand_side=hs)
        self.p_cur=PlotPanel("Current","mA",(0,2000),hand_side=hs)
        pt.addTab(self.p_ang,"📐 Angle"); pt.addTab(self.p_frc,"💪 Force"); pt.addTab(self.p_cur,"⚡ Current")
        pt.setMinimumHeight(150); bot.addWidget(pt)

        tbox=QGroupBox("🖐 Tactile  [blue = no press  →  red = hard press]")
        tc=QHBoxLayout(tbox)
        fi=[("Pinky",["pinky_tip","pinky_nail","pinky_pad"]),
            ("Ring", ["ring_tip","ring_nail","ring_pad"]),
            ("Mid",  ["middle_tip","middle_nail","middle_pad"]),
            ("Index",["index_tip","index_nail","index_pad"]),
            ("Thumb",["thumb_tip","thumb_nail","thumb_mid","thumb_pad"])]
        self._ftac=[]
        for n,ks in fi:
            w=FingerTacWidget(n,ks); self._ftac.append(w); tc.addWidget(w)
        self._ptac=PalmTacWidget(); tc.addWidget(self._ptac)
        tc.addWidget(ColorBarWidget())
        tbox.setMinimumHeight(110); bot.addWidget(tbox)
        bot.setSizes([210,130])

        # ── vertical splitter wrapping top and bot for draggable divider ────────
        vsplit=QSplitter(Qt.Vertical); vsplit.setHandleWidth(8)
        vsplit.addWidget(top); vsplit.addWidget(bot)
        vsplit.setSizes([320, 360])
        root.addWidget(vsplit, 1)

    def on_state(self,s):
        self.mj_widget.set_angles(s.get('angle',[0]*6), instant=True)
        self.tbl.update_state(s)
        _t = s.get('_t')
        self.p_ang.push(s.get('angle',[0]*6), _t)
        self.p_frc.push(s.get('force',[0]*6), _t)
        self.p_cur.push(s.get('current',[0]*6), _t)

    def on_tac(self,td):
        for w in self._ftac: w.update_data(td)
        self._ptac.update_data(td)
        self.mj_widget.set_tactile(td)

# ──────────────────────────────────────────────────────────────────────────────
#  DASHBOARD TAB
# ──────────────────────────────────────────────────────────────────────────────
class _VerticalTab(QPushButton):
    """Narrow sidebar-tab style handle with rotated text, used by SlideDrawer."""
    def __init__(self, text: str, width: int = 18, parent=None):
        super().__init__(parent)
        self._text = text
        self.setFixedWidth(width)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            f"QPushButton{{background:{PAL['input']};border:1px solid {PAL['border']};border-radius:4px;}}"
            f"QPushButton:hover{{background:{PAL['border']};}}")

    def sizeHint(self):
        return QSize(self.width(), 140)

    def paintEvent(self, ev):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(PAL['input']))
        painter.setPen(QColor(PAL['text']))
        f = QFont(); f.setBold(True); f.setPointSize(7); painter.setFont(f)
        painter.translate(self.width()/2, self.height()/2)
        painter.rotate(-90)
        rect = QRect(-self.height()//2, -self.width()//2, self.height(), self.width())
        painter.drawText(rect, Qt.AlignCenter, self._text)
        painter.end()


class SlideDrawer(QWidget):
    """
    A collapsible panel that slides out from an EDGE of the WHOLE app
    window, leaving only a narrow handle tab visible when collapsed. Click
    the handle to toggle. Positioned absolutely (not part of the host's
    normal layout) so it can overlay on top of existing content.

    Anchored to the FULL DashboardTab (not an individual hand's panel) by
    design: each hand's drawer opens from the edge AWAY from its own panel,
    so expanding it overlays the OPPOSITE hand's panel — which you aren't
    watching while you're busy adjusting THIS hand — rather than covering
    the very panel you need to see while dragging its own sliders.

    edge='right' → handle on the LEFT of the drawer, collapses by sliding
                   right until only the handle shows flush at the window's
                   right edge (used for the LEFT hand, per request).
    edge='left'  → handle on the RIGHT of the drawer, collapses by sliding
                   left until only the handle shows flush at the window's
                   left edge (used for the RIGHT hand, per request).

    The drawer (content) itself always spans the FULL host height at y=0 —
    only the HANDLE's own vertical position within it is offset (via
    HANDLE_TOP_OFFSET below), so the control panel's size/position never
    changes, just where its little name-tag sits.
    """
    CONTENT_W = 460
    HANDLE_W  = 18
    HANDLE_H  = 110
    HANDLE_TOP_OFFSET = 128   # vertical offset of the HANDLE ONLY, within the
                               # full-height drawer — change this to move just
                               # the name tag up/down without affecting the
                               # control panel's own size or position at all

    def __init__(self, host: QWidget, edge: str, content: QWidget,
                 handle_text: str = "CONTROL", parent=None):
        super().__init__(parent or host)
        self.host = host
        self.edge = edge
        self._open = False
        # Deliberately NO background/border on the drawer itself — only the
        # handle (below) has its own visible background. Otherwise the
        # full-height collapsed sliver shows as one continuous bar behind
        # the handle instead of just a small tag.
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

        lay = QHBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)

        # Wrap the handle in its own column with a top spacer, so the
        # HANDLE's position can be offset independently of the content's
        # height/position (which stays exactly as it always was).
        handle_col = QWidget()
        handle_col.setAttribute(Qt.WA_TranslucentBackground, True)
        hc_lay = QVBoxLayout(handle_col); hc_lay.setContentsMargins(0,0,0,0); hc_lay.setSpacing(0)
        self.handle = _VerticalTab(handle_text)
        self.handle.setFixedHeight(self.HANDLE_H)
        self.handle.clicked.connect(self.toggle)
        hc_lay.addSpacing(self.HANDLE_TOP_OFFSET)
        hc_lay.addWidget(self.handle)
        hc_lay.addStretch(1)

        if edge == 'right':
            lay.addWidget(handle_col)
            lay.addWidget(content, 1)
        else:
            lay.addWidget(content, 1)
            lay.addWidget(handle_col)

        self.setFixedWidth(self.CONTENT_W + self.HANDLE_W)
        self.raise_()
        self._reposition(animate=False)
        host.installEventFilter(self)   # reposition on host resize

    def eventFilter(self, obj, ev):
        if obj is self.host and ev.type() == QEvent.Resize:
            self._reposition(animate=False)
        return False

    def toggle(self):
        self._open = not self._open
        self._reposition(animate=True)

    def _reposition(self, animate: bool):
        h = self.host.height()           # full height, same as before — unaffected by handle offset
        self.setFixedHeight(h)
        hw = self.host.width()
        if self.edge == 'right':
            target_x = (hw - self.CONTENT_W - self.HANDLE_W) if self._open else (hw - self.HANDLE_W)
        else:
            target_x = 0 if self._open else (-self.CONTENT_W)
        if animate:
            anim = QPropertyAnimation(self, b"pos", self)
            anim.setDuration(220)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.setStartValue(self.pos())
            anim.setEndValue(QPoint(target_x, 0))
            anim.start()
            self._anim = anim   # keep a reference so it isn't garbage-collected mid-animation
        else:
            self.move(target_x, 0)


class DashboardTab(QWidget):
    send_ang = pyqtSignal(int, list)
    send_frc = pyqtSignal(int, list)
    send_spd = pyqtSignal(int, list)

    def __init__(self,sessions:List[HandSession],parent=None):
        super().__init__(parent)
        lay=QVBoxLayout(self); lay.setContentsMargins(0,0,0,0)
        self.split=QSplitter(Qt.Horizontal); self.split.setHandleWidth(6)
        self.panels:List[HandDashPanel]=[]
        for sess in sessions:
            p=HandDashPanel(sess); self.panels.append(p); self.split.addWidget(p)
        self.split.setSizes([800,800])
        lay.addWidget(self.split)

        # ── Hidden slide-out Control drawer per hand ───────────────────────
        # Anchored to the WHOLE dashboard tab (the app window), not the
        # individual panel — so expanding it overlays the OPPOSITE hand's
        # panel (not being watched while you adjust THIS hand) instead of
        # covering the very panel you need to see while dragging its own
        # sliders. Left hand's drawer opens from the window's right edge;
        # Right hand's drawer opens from the window's left edge.
        self._drawer_controls: List[SingleHandControl] = []
        self._drawers: List[SlideDrawer] = []
        for i, sess in enumerate(sessions):
            ctrl = SingleHandControl(sess, compact=True)
            ctrl.send_ang.connect(lambda v,idx=i: self.send_ang.emit(idx, v))
            ctrl.send_frc.connect(lambda v,idx=i: self.send_frc.emit(idx, v))
            ctrl.send_spd.connect(lambda v,idx=i: self.send_spd.emit(idx, v))
            self._drawer_controls.append(ctrl)
            edge = 'right' if sess.name == 'Left' else 'left'
            drawer = SlideDrawer(self, edge, ctrl, handle_text=f"{sess.name[0]} CONTROL")
            self._drawers.append(drawer)

    def on_state(self,idx,s):
        if idx<len(self.panels): self.panels[idx].on_state(s)
        if idx<len(self._drawer_controls): self._drawer_controls[idx].update_state(s)
    def on_tac(self,idx,td):
        if idx<len(self.panels): self.panels[idx].on_tac(td)
        if idx<len(self._drawer_controls): self._drawer_controls[idx].update_tac(td)
class TactileTab(QWidget):
    def __init__(self,sessions:List[HandSession],parent=None):
        super().__init__(parent)
        lay=QVBoxLayout(self); lay.setContentsMargins(0,0,0,0)
        hrow=QHBoxLayout(); hrow.addWidget(QLabel("Show:"))
        self.cb=QComboBox()
        for s in sessions: self.cb.addItem(f"{s.name} Hand")
        self.cb.currentIndexChanged.connect(lambda i:self._stk.setCurrentIndex(i))
        hrow.addWidget(self.cb); hrow.addStretch(); lay.addLayout(hrow)
        self._stk=QStackedWidget(); lay.addWidget(self._stk,1)
        self._ftacs:List[List[FingerTacWidget]]=[]
        self._ptacs:List[PalmTacWidget]=[]
        self._tac_splitters:List[QSplitter]=[]
        # DOF index mapping for tactile fingers: Pinky=0, Ring=1, Middle=2, Index=3, Thumb=4
        fi_dof=[("Pinky",["pinky_tip","pinky_nail","pinky_pad"],0),
                ("Ring", ["ring_tip","ring_nail","ring_pad"],1),
                ("Middle",["middle_tip","middle_nail","middle_pad"],2),
                ("Index",["index_tip","index_nail","index_pad"],3),
                ("Thumb",["thumb_tip","thumb_nail","thumb_mid","thumb_pad"],4)]
        for sess in sessions:
            side = 'R' if sess.name=='Right' else 'L'
            page=QWidget(); pl=QHBoxLayout(page); pl.setContentsMargins(0,0,0,0)
            splitter=QSplitter(Qt.Horizontal); splitter.setHandleWidth(6)
            wids=[]
            for n,ks,di in fi_dof:
                gb=QGroupBox(); gbl=QVBoxLayout(gb)
                # Header: icon + name
                hdr=QHBoxLayout(); hdr.setSpacing(4)
                hdr.addWidget(FingerIcon(di,28,side))
                lbl=QLabel(n); lbl.setStyleSheet(f"color:{DOF_COLORS[di]};font-weight:bold;font-size:{FS}pt;")
                hdr.addWidget(lbl); hdr.addStretch()
                gbl.addLayout(hdr)
                w=FingerTacWidget(n,ks); gbl.addWidget(w,1)
                wids.append(w); gb.setMinimumWidth(60); splitter.addWidget(gb)
            self._ftacs.append(wids)
            pb=QGroupBox("Palm"); pbl=QVBoxLayout(pb)
            ptw=PalmTacWidget(); pbl.addWidget(ptw); self._ptacs.append(ptw)
            pb.setMinimumWidth(60); splitter.addWidget(pb)
            splitter.setSizes([110]*5 + [210])  # finger cols x5, then Palm width (this last number)
            splitter.splitterMoved.connect(
                lambda pos,idx,sp=splitter: self._on_tactile_resize(sp))
            self._tac_splitters.append(splitter)
            pl.addWidget(splitter,1)
            pl.addWidget(ColorBarWidget()); self._stk.addWidget(page)
        self.cb.setCurrentIndex(1)

    def _on_tactile_resize(self, sp: 'QSplitter'):
        sizes = sp.sizes()
        if len(sizes) != 6: return
        total = sum(sizes)
        palm = sizes[5]
        remaining = max(60*5, total - palm)   # don't starve the fingers below min
        each = remaining // 5
        new_sizes = [each]*5
        new_sizes.append(total - each*5)       # give rounding remainder to palm
        sp.blockSignals(True)
        sp.setSizes(new_sizes)
        sp.blockSignals(False)

    def on_tac(self,idx,td):
        if idx<len(self._ftacs):
            for w in self._ftacs[idx]: w.update_data(td)
            self._ptacs[idx].update_data(td)

# ──────────────────────────────────────────────────────────────────────────────
#  DOF CONTROL ROW
# ──────────────────────────────────────────────────────────────────────────────
class DOFRow(QWidget):
    changed=pyqtSignal(int,int)
    def __init__(self,idx,name,color,hand_side='L',parent=None,compact=False):
        super().__init__(parent); self.idx=idx
        lay=QHBoxLayout(self); lay.setContentsMargins(3,1,3,1)
        # Finger icon (from user images, uniform scale)
        icon=FingerIcon(idx, 38, hand_side); lay.addWidget(icon)
        # Finger name
        lbl=QLabel(name); lbl.setFixedWidth(68)
        lbl.setStyleSheet(f"color:{color};font-weight:bold;font-size:{FS}pt;"); lay.addWidget(lbl)
        self.sl=QSlider(Qt.Horizontal); self.sl.setRange(0,1000); self.sl.setValue(1000)
        self.sl.setMinimumHeight(22)
        self.sl.setStyleSheet(f"""
            QSlider::groove:horizontal{{background:{PAL['input']};height:7px;border-radius:3px;}}
            QSlider::handle:horizontal{{background:{color};width:16px;height:16px;
                border-radius:8px;margin:-5px 0;}}
            QSlider::sub-page:horizontal{{background:{color};border-radius:3px;}}""")
        lay.addWidget(self.sl,1)
        self.vl=QLabel("1000"); self.vl.setFixedWidth(36)
        self.vl.setAlignment(Qt.AlignCenter)
        self.vl.setStyleSheet(f"color:{PAL['dim']};font-size:{FS}pt;font-weight:bold;"); lay.addWidget(self.vl)
        self.al=QLabel("—"); self.al.setFixedWidth(54)
        self.al.setStyleSheet(f"color:#58a6ff;font-size:{FS_S}pt;"); lay.addWidget(self.al)
        self.fb=QProgressBar(); self.fb.setRange(0,4000); self.fb.setValue(0)
        self.fb.setFixedWidth(80); self.fb.setFixedHeight(8); self.fb.setTextVisible(False)
        self.fb.setStyleSheet(f"QProgressBar{{background:{PAL['input']};border-radius:4px;}}QProgressBar::chunk{{background:{color};border-radius:4px;}}")
        if not compact:
            lay.addWidget(self.fb)   # hidden in compact (drawer) mode, widget still exists for set_actual()
        self.sl.valueChanged.connect(lambda v:(self.vl.setText(str(v)),self.changed.emit(self.idx,v)))
        self.force_cb=QCheckBox()
        self.force_cb.setToolTip("Move together: include this finger when dragging another checked finger")
        self.force_cb.setFixedWidth(18)
        lay.addWidget(self.force_cb)
        self.stop_force_cb=QCheckBox("F"); self.stop_force_cb.setChecked(True)
        self.stop_force_cb.setToolTip("Stop this finger when its FORCE reading reaches the force-limit spinbox")
        self.stop_force_cb.setFixedWidth(28)
        lay.addWidget(self.stop_force_cb)
        self.stop_tac_cb=QCheckBox("T"); self.stop_tac_cb.setChecked(True)
        self.stop_tac_cb.setToolTip("Stop this finger when its TACTILE reading reaches the tactile-threshold spinbox")
        self.stop_tac_cb.setFixedWidth(28)
        lay.addWidget(self.stop_tac_cb)
    def set_actual(self,a,f):
        self.al.setText(f"↔{a}"); self.fb.setValue(abs(f))

# ──────────────────────────────────────────────────────────────────────────────
#  SINGLE-HAND CONTROL  (sliders + optional 3D/sketch side panel)
# ──────────────────────────────────────────────────────────────────────────────
class SingleHandControl(QWidget):
    send_ang=pyqtSignal(list); send_frc=pyqtSignal(list); send_spd=pyqtSignal(list)
    sim_only_toggled=pyqtSignal(bool)  # tells ControlTab (-> MainWindow) to disconnect/stop this hand

    def __init__(self,sess:HandSession,parent=None,compact=False):
        super().__init__(parent)
        self.sess=sess; self._ang=[1000]*6; self._curr_forces=[0]*6; self._curr_tac=[0]*6
        self._hw_synced=False  # one-time: snap sliders to the hand's actual pose on first state update
        self._compact = compact
        root=QHBoxLayout(self); root.setContentsMargins(0,0,0,0)

        # ── left: sliders + gestures ──────────────────────────────────────
        left=QWidget(); ll=QVBoxLayout(left); ll.setSpacing(6)
        ab=QGroupBox("🖐  Finger Angles   0=closed  ·  1000=open")
        al=QVBoxLayout(ab); self._rows=[]
        side = 'R' if sess.name=='Right' else 'L'
        for i in range(6):
            r=DOFRow(i,DOF_NAMES[i],DOF_COLORS[i],side,compact=compact)
            r.changed.connect(self._on_dof_changed)   # real-time send
            al.addWidget(r); self._rows.append(r)
        brow=QHBoxLayout()
        for lbl,ang,col,gkey in [
            ("✋ Open",  [1000,1000,1000,1000,300,1000], PAL['btn_neutral'], None),
            ("✊ Fist",  [50,50,50,50,200,800],           PAL['btn_neutral'], None),
            ("👆 Point", [0,0,0,1000,0,600],               PAL['btn_neutral'], None),
            ("👌 OK",   [1000,900,800,400,630,0],          PAL['btn_neutral'], None),
            ("👍 Up",   [0,0,0,0,1000,1000],              PAL['btn_neutral'], None),
            ("🤘 Rock", [1000,0,0,1000,0,300],             PAL['btn_neutral'], None),
        ]:
            if gkey:
                # Icon button for Point / Victory
                b = QPushButton()
                b.setStyleSheet(
                    f"QPushButton{{background:{col};border:1px solid {PAL['border']};"
                    f"border-radius:4px;padding:2px 6px;font-size:{FS}pt;}}"
                    f"QPushButton:hover{{border-color:#58a6ff;}}")
                bl2 = QVBoxLayout(b); bl2.setContentsMargins(3,2,3,2); bl2.setSpacing(1)
                icon_w = FingerIcon.gesture(gkey, 30, side)
                bl2.addWidget(icon_w, 0, Qt.AlignCenter)
                lbl_w = QLabel(lbl); lbl_w.setAlignment(Qt.AlignCenter)
                lbl_w.setStyleSheet(f"color:{PAL['text']};font-size:{FS_S}pt;background:transparent;")
                bl2.addWidget(lbl_w)
                b.clicked.connect(lambda _,a=ang:self._preset(a))
            else:
                b = _mk_btn(lbl, col, bold=(col == "#238636"))
                if ang is not None: b.clicked.connect(lambda _,a=ang:self._preset(a))
                else: b.clicked.connect(self._send)
            brow.addWidget(b)
        al.addLayout(brow)

        # Checkbox hint label (force-move now triggers directly from slider drag)
        hint=QLabel("☑ Check fingers to move them together — each stops at its own force limit (set below)")
        hint.setStyleSheet(f"color:{PAL['dim']};font-size:{FS_S}pt;")
        hint.setWordWrap(True)
        al.addWidget(hint)
        ll.addWidget(ab)

        # Force + Speed
        fs=QSplitter(Qt.Horizontal); fs.setHandleWidth(5)
        fb=QGroupBox("\U0001f4aa Force (g) & Tactile Threshold"); fl=QGridLayout(fb)
        self._fsp=[]; self._tsp=[]
        fl.addWidget(QLabel("DOF"),0,0)
        fl.addWidget(QLabel("Force limit (g)"),0,1)
        fl.addWidget(QLabel("Tactile thresh"),0,2)
        for i in range(6):
            fl.addWidget(QLabel(DOF_NAMES[i]),i+1,0)
            sp=QSpinBox(); sp.setRange(0,3000); sp.setValue(500); sp.setSuffix("g")
            fl.addWidget(sp,i+1,1); self._fsp.append(sp)
            tp=QSpinBox(); tp.setRange(0,4095); tp.setValue(500)
            tp.setToolTip("Tactile threshold: stop finger when any sensor on that finger exceeds this value")
            fl.addWidget(tp,i+1,2); self._tsp.append(tp)
        b2=_mk_btn("Send Forces","#1f6feb"); b2.clicked.connect(lambda:self.send_frc.emit([s.value() for s in self._fsp])); fl.addWidget(b2,7,0,1,3); fs.addWidget(fb)
        spb=QGroupBox("🚀 Speed"); sl2=QGridLayout(spb)
        self._ssp=[]; sl2.addWidget(QLabel("DOF"),0,0); sl2.addWidget(QLabel("0-1000"),0,1)
        for i in range(6):
            sl2.addWidget(QLabel(DOF_NAMES[i]),i+1,0)
            sp=QSpinBox(); sp.setRange(0,1000); sp.setValue(1000); sl2.addWidget(sp,i+1,1); self._ssp.append(sp)
        b3=_mk_btn("Send Speeds","#1f6feb"); b3.clicked.connect(lambda:self.send_spd.emit([s.value() for s in self._ssp])); sl2.addWidget(b3,7,0,1,2); fs.addWidget(spb)
        ll.addWidget(fs)

        # ── right: MuJoCo simulation (skipped in compact/drawer mode) ──────
        self._mj_ctrl = None
        if not compact:
            right=QGroupBox("🤖 Hand Sim (MuJoCo)")
            rl=QVBoxLayout(right)
            _here=Path(__file__).parent if "__file__" in dir() else Path(".")
            _mj_side="right" if sess.name=="Right" else "left"
            _mj_def=next((str(p) for p in [
                _here/f"inspire_hand_{_mj_side}.xml",
                _here/f"rh56dftp_{_mj_side}.xml"] if p.exists()),"")
            self._mj_ctrl=MuJoCoWidget(sess.name, _mj_def)
            self._sim_only_cb = QCheckBox("🎓 Simulation-only (no hand needed) — drive sliders straight into the sim")
            self._sim_only_cb.setToolTip(
                "For learning/demo purposes: while checked, slider and preset\n"
                "moves drive this simulation directly and are NOT sent to the\n"
                "real hand or demo generator. Uncheck to resume normal control.\n"
                "Physical-hand control on this tab is otherwise unaffected.")
            self._sim_only_cb.toggled.connect(self._on_sim_only_toggled)
            rl.addWidget(self._sim_only_cb)
            rl.addWidget(self._mj_ctrl, 1)
            right.setMinimumWidth(280)

            splitter=QSplitter(Qt.Horizontal); splitter.setHandleWidth(6)
            splitter.addWidget(left); splitter.addWidget(right)
            splitter.setSizes([500,500])   # hand view takes ~half the panel width
            root.addWidget(splitter)
        else:
            root.addWidget(left)
        self._vis3d=None; self._vis2d=None  # kept for interface compat

    def _update_ang(self,i,v): self._ang[i]=v

    def _limits_active(self) -> bool:
        """True only when force/tactile stop-limits should be enforced.

        The limits gate on real sensor readings, so they apply only to a
        physically connected hand -- not to Demo mode (synthetic forces) or
        Simulation-only mode (no sensors)."""
        if getattr(self, '_sim_only_cb', None) is not None and self._sim_only_cb.isChecked():
            return False
        sess = getattr(self, 'sess', None)
        if sess is None:
            return False
        if getattr(sess, 'demo_mode', False):
            return False
        return bool(getattr(sess, 'connected', False) or getattr(sess, 'poller', None))

    def _stop_blocks(self, j, requested_v, cur_v):
        """True if finger j should be blocked from moving to requested_v.
        Only ever blocks FURTHER CLOSING (requested_v < cur_v) once at/over
        its own force/tactile limit — opening/relieving is NEVER blocked,
        regardless of the current force/tactile reading."""
        if requested_v >= cur_v:
            return False
        if not self._limits_active():
            return False
        r = self._rows[j]
        if r.stop_force_cb.isChecked() and abs(self._curr_forces[j]) >= self._fsp[j].value():
            return True
        if r.stop_tac_cb.isChecked() and self._curr_tac[j] >= self._tsp[j].value():
            return True
        return False

    def _approach_clamp(self, j, requested_v, cur_v):
        """Clamp a closing step to SimTuning.force_approach_step once
        finger j is within SimTuning.force_approach_margin of its force or
        tactile limit, so it eases in rather than overshooting."""
        if requested_v >= cur_v:
            return requested_v   # opening — never clamped
        if not self._limits_active():
            return requested_v
        r = self._rows[j]
        margin = SimTuning.force_approach_margin
        near_limit = False
        if r.stop_force_cb.isChecked():
            if self._fsp[j].value() - abs(self._curr_forces[j]) < margin:
                near_limit = True
        if r.stop_tac_cb.isChecked():
            if self._tsp[j].value() - self._curr_tac[j] < margin:
                near_limit = True
        if near_limit:
            step = max(1, SimTuning.force_approach_step)
            if cur_v - requested_v > step:
                return cur_v - step
        return requested_v

    def _on_dof_changed(self, idx, v):
        """Slider moved — send immediately. If this finger's ☑ checkbox is
        checked, every OTHER checked finger also moves by the SAME DELTA
        from ITS OWN current position (not snapped to v).
        Each finger's F/T checkboxes choose which condition(s) freeze it:
        F = force feedback >= force-limit spinbox, T = tactile reading >=
        tactile-threshold spinbox. Both can be checked together. Opening
        (relieving) is always allowed regardless of the current reading —
        only further closing past the limit is ever blocked. As any
        checked finger nears its own limit it eases in via
        SimTuning.force_approach_step instead of jumping straight past it,
        so it actually stops near the configured number instead of
        wherever the mouse-drag happened to land."""
        old_v = self._ang[idx]

        # The dragged finger respects its own stop/ease-in, same as synced ones.
        v = self._approach_clamp(idx, v, old_v)
        if self._stop_blocks(idx, v, old_v):
            v = old_v
        if self._rows[idx].sl.value() != v:
            self._rows[idx].sl.blockSignals(True); self._rows[idx].sl.setValue(v); self._rows[idx].sl.blockSignals(False)
            self._rows[idx].vl.setText(str(v))
        self._update_ang(idx, v)
        delta = v - old_v

        if delta != 0 and self._rows[idx].force_cb.isChecked():
            for j, r in enumerate(self._rows):
                if j == idx or not r.force_cb.isChecked(): continue
                cur_v = self._ang[j]
                requested = max(0, min(1000, cur_v + delta))
                requested = self._approach_clamp(j, requested, cur_v)
                if self._stop_blocks(j, requested, cur_v): continue
                r.sl.blockSignals(True); r.sl.setValue(requested); r.sl.blockSignals(False)
                r.vl.setText(str(requested)); self._ang[j] = requested
        self._send()

    def _preset(self,a):
        for r in self._rows: r.sl.blockSignals(True)
        for i,(r,v) in enumerate(zip(self._rows,a)):
            r.sl.setValue(v); r.vl.setText(str(v)); self._ang[i]=v
        for r in self._rows: r.sl.blockSignals(False)
        self._send()

    def _send(self):
        if getattr(self, '_sim_only_cb', None) is not None and self._sim_only_cb.isChecked():
            if self._mj_ctrl is not None:
                self._mj_ctrl.set_angles(list(self._ang), instant=True)
            return  # sim-only: never reaches real hardware or demo generator
        self.send_ang.emit(list(self._ang))

    def _on_sim_only_toggled(self, checked):
        if checked and self._mj_ctrl is not None and self._mj_ctrl._rend is None:
            self._mj_ctrl._start()
        self.sim_only_toggled.emit(checked)

    def reset_live_readings(self):
        """Zero the cached force/tactile readings used by _stop_blocks,
        so no reading from a previous session carries over."""
        self._curr_forces = [0]*6
        self._curr_tac = [0]*6
        for r in self._rows:
            r.set_actual(0, 0)

    def sync_from(self, angles: list):
        """Mirror ALL 6 slider positions (visual + internal _ang) from
        another SingleHandControl's full angle state — e.g. Control tab
        mirroring the Dashboard drawer or vice versa. Uses the FULL state
        (not a single per-slider signal) because group-sync moves OTHER
        fingers with blockSignals(True) internally, which would silently
        starve a naive per-slider valueChanged link."""
        for i, r in enumerate(self._rows):
            v = angles[i]
            if r.sl.value() != v:
                r.sl.blockSignals(True); r.sl.setValue(v); r.sl.blockSignals(False)
                r.vl.setText(str(v))
            self._ang[i] = v

    # DOF index → tactile sensor key(s) mapping
    _TAC_KEYS = [
        ['pinky_tip','pinky_nail','pinky_pad'],
        ['ring_tip','ring_nail','ring_pad'],
        ['middle_tip','middle_nail','middle_pad'],
        ['index_tip','index_nail','index_pad'],
        ['thumb_tip','thumb_nail','thumb_mid','thumb_pad'],
        ['thumb_tip','thumb_nail','thumb_mid','thumb_pad'],  # DOF5 (thumb rot) shares thumb sensors
    ]

    def update_state(self,s):
        self._curr_forces=s.get('force',[0]*6)
        angle = s.get('angle',[0]*6)
        if not self._hw_synced:
            self._hw_synced = True
            for i, r in enumerate(self._rows):
                r.sl.blockSignals(True); r.sl.setValue(angle[i]); r.sl.blockSignals(False)
                r.vl.setText(str(angle[i])); self._ang[i] = angle[i]
        for i,r in enumerate(self._rows): r.set_actual(angle[i],s.get('force',[0]*6)[i])
        if self._mj_ctrl: self._mj_ctrl.set_angles(angle, instant=True)

    def update_tac(self, td):
        """Extract per-DOF peak tactile value so the slider group-move
        logic can freeze a finger when it touches something."""
        for dof, keys in enumerate(self._TAC_KEYS):
            peak = 0
            for k in keys:
                arr = td.get(k)
                if arr is not None and hasattr(arr, 'max'):
                    peak = max(peak, int(arr.max()))
            self._curr_tac[dof] = peak
        if self._mj_ctrl: self._mj_ctrl.set_tactile(td)

# ──────────────────────────────────────────────────────────────────────────────
#  CONTROL TAB
# ──────────────────────────────────────────────────────────────────────────────
class ControlTab(QWidget):
    send_ang=pyqtSignal(int,list); send_frc=pyqtSignal(int,list); send_spd=pyqtSignal(int,list)
    disconnect_req=pyqtSignal(int)  # relayed from a panel's Simulation-only toggle
    def __init__(self,sessions:List[HandSession],parent=None):
        super().__init__(parent)
        lay=QVBoxLayout(self); self._mj_widgets = []; lay.setContentsMargins(0,0,0,0)
        tabs=QTabWidget(); self._panels:List[SingleHandControl]=[]
        for i,sess in enumerate(sessions):
            p=SingleHandControl(sess)
            p.send_ang.connect(lambda v,i=i:self.send_ang.emit(i,v))
            p.send_frc.connect(lambda v,i=i:self.send_frc.emit(i,v))
            p.send_spd.connect(lambda v,i=i:self.send_spd.emit(i,v))
            p.sim_only_toggled.connect(lambda checked,i=i: self.disconnect_req.emit(i) if checked else None)
            tabs.addTab(p,f"{sess.name} Hand"); self._panels.append(p)
        both=QWidget(); bl=QVBoxLayout(both)
        bl.addWidget(QLabel("Preset gestures sent to BOTH hands simultaneously:"))
        gest=[("✋ Open",[1000,1000,1000,1000,300,1000]),("✊ Fist",[50,50,50,50,200,800]),
              ("👆 Point",[0,0,0,1000,0,600]),("✌️ Victory",[0,0,1000,1000,0,600]),
              ("👍 Thumbs Up",[0,0,0,0,1000,1000]),("🤘 Rock",[1000,0,0,1000,0,300]),
              ("🖖 Vulcan",[1000,1000,1000,1000,1000,1000]),("👌 OK",[1000,900,800,400,630,0])]
        gg=QGridLayout()
        gesture_keys = {"👆 Point":"point","✌️ Victory":"Victory"}
        for idx,(lbl,ang) in enumerate(gest):
            b=_mk_btn(lbl, PAL['btn_neutral'])
            b.clicked.connect(lambda _,a=ang:self._both(a))
            gg.addWidget(b, idx//4, idx%4)
        bl.addLayout(gg); bl.addStretch()
        tabs.addTab(both,"🤲 Both"); lay.addWidget(tabs)
    def _both(self,a):
        for i in range(len(self._panels)): self.send_ang.emit(i,a)
    def on_state(self,idx,s):
        # The full update_state() is needed, not just the sim view: it snaps
        # the sliders to the hand's real pose after connecting (otherwise the
        # first slider move sends stale values for the other five fingers)
        # and feeds the live forces that the F stop-limit checks.
        if idx >= len(self._panels): return
        sim_only_cb = getattr(self._panels[idx], '_sim_only_cb', None)
        if sim_only_cb is None or not sim_only_cb.isChecked():
            self._panels[idx].update_state(s)
    def on_tac(self,idx,td):
        if idx<len(self._panels): self._panels[idx].update_tac(td)

# ──────────────────────────────────────────────────────────────────────────────
#  ACTION TAB
# ──────────────────────────────────────────────────────────────────────────────
STEP_HDR=['Pinky','Ring','Middle','Index','Th.Bend','Th.Rot','Speed','Force','Delay ms']
# DOF indices for the first 6 columns
_STEP_DOF = [0, 1, 2, 3, 4, 5, -1, -1, -1]

class ActionTab(QWidget):
    execute_action=pyqtSignal(int,list)
    def __init__(self,sessions:List[HandSession],parent=None):
        super().__init__(parent)
        self.sessions=sessions; self.db=[[] for _ in range(40)]; self._slot=0
        root=QHBoxLayout(self); root.setContentsMargins(3,3,3,3)

        # slot list
        lb=QGroupBox("Slots  (1-13 fixed)"); lb.setMinimumWidth(175); lb.setMaximumWidth(200)
        ll=QVBoxLayout(lb)
        self.sl=QTableWidget(40,2)
        self.sl.setHorizontalHeaderLabels(["#","n"])
        self.sl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.sl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.sl.setSelectionBehavior(QTableWidget.SelectRows)
        for i in range(40):
            it0=QTableWidgetItem(f"Act {i+1:02d}")
            it0.setForeground(QColor('#8b949e' if i<13 else '#58a6ff'))
            it1=QTableWidgetItem("0")
            self.sl.setItem(i,0,it0); self.sl.setItem(i,1,it1)
        self.sl.currentItemChanged.connect(self._sel_changed)
        ll.addWidget(self.sl); root.addWidget(lb)

        # editor
        right=QVBoxLayout()
        hrow=QHBoxLayout()
        self.lbl=QLabel("← Select slot"); self.lbl.setStyleSheet(f"color:#58a6ff;font-size:{FS_L}pt;font-weight:bold;")
        hrow.addWidget(self.lbl); hrow.addSpacing(20)
        hrow.addWidget(QLabel("Send to:")); self.hcb=QComboBox()
        for s in sessions: self.hcb.addItem(s.name)
        self.hcb.addItem("Both"); hrow.addWidget(self.hcb); hrow.addStretch()
        right.addLayout(hrow)

        # step table
        self.stbl=QTableWidget(8,len(STEP_HDR))
        self.stbl.setVerticalHeaderLabels([f"Step {i+1}" for i in range(8)])
        self.stbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        # Add finger icons + coloured names to first 6 headers
        for c,(hdr_lbl,di) in enumerate(zip(STEP_HDR,_STEP_DOF)):
            if di>=0:
                w=QWidget(); wl=QVBoxLayout(w); wl.setContentsMargins(1,1,1,0); wl.setSpacing(0)
                wl.addWidget(FingerIcon(di,24,'L'),0,Qt.AlignCenter)
                lbl2=QLabel(hdr_lbl); lbl2.setAlignment(Qt.AlignCenter)
                lbl2.setStyleSheet(f"color:{DOF_COLORS[di]};font-size:{FS_S-1}pt;font-weight:bold;")
                wl.addWidget(lbl2)
                self.stbl.horizontalHeader().setSectionResizeMode(c,QHeaderView.Fixed)
                self.stbl.setColumnWidth(c,46)
                self.stbl.setHorizontalHeaderItem(c,QTableWidgetItem(hdr_lbl))
                # place widget in header via a workaround: use row -1 section widget
                self.stbl.horizontalHeader().setMinimumSectionSize(38)
            else:
                self.stbl.setHorizontalHeaderItem(c,QTableWidgetItem(hdr_lbl))
        for r in range(8):
            for c in range(len(STEP_HDR)): self.stbl.setItem(r,c,QTableWidgetItem(""))
        right.addWidget(self.stbl,1)

        brow=QHBoxLayout()
        for lbl,fn,col in [
            ("+ Step",self._add,PAL['btn_neutral']),("- Step",self._del,PAL['btn_neutral']),
            ("▶ Execute",self._exec,"#238636"),("💾 Save",self._save,"#1f6feb"),
            ("📂 Load",self._load,"#1f6feb"),("🗑 Clear",self._clr,"#da3633")]:
            b=_mk_btn(lbl,col,bold=(col=="#238636")); b.clicked.connect(fn); brow.addWidget(b)
        right.addLayout(brow)
        right.addWidget(QLabel("💡 Empty=hold · 0=closed · 1000=open · -1=hold · Delay in ms"))
        root.addLayout(right)

    def _sel_changed(self,cur,_):
        if cur is None: return
        r=self.sl.row(cur)
        if r<0: return
        self._slot=r; self.lbl.setText(f"Slot {r+1}  {'(Fixed)' if r<13 else '(Custom)'}"); self._refresh()

    def _refresh(self):
        steps=self.db[self._slot]
        for r in range(8):
            if r<len(steps):
                s=steps[r]
                for c in range(6): self.stbl.item(r,c).setText(str(s['angles'][c]) if s['angles'][c]>=0 else "")
                for c,k in [(6,'speed'),(7,'force'),(8,'delay')]: self.stbl.item(r,c).setText(str(s.get(k,500)))
            else:
                for c in range(len(STEP_HDR)): self.stbl.item(r,c).setText("")
        self._upd_list()

    def _parse(self):
        out=[]
        for r in range(8):
            angs=[]
            for c in range(6):
                t=self.stbl.item(r,c).text().strip()
                try: v=max(-1,min(1000,int(t)))
                except: v=-1
                angs.append(v)
            if not any(a>=0 for a in angs): continue
            def gv(col,lo,hi,df):
                try: return max(lo,min(hi,int(self.stbl.item(r,col).text())))
                except: return df
            out.append(dict(angles=angs,speed=gv(6,0,1000,500),force=gv(7,0,3000,500),delay=gv(8,0,60000,500)))
        return out

    def _add(self):
        s=self._parse()
        if len(s)<8: s.append(dict(angles=[-1]*6,speed=500,force=500,delay=500))
        self.db[self._slot]=s; self._refresh()
    def _del(self):
        s=self._parse()
        if s: s.pop()
        self.db[self._slot]=s; self._refresh()
    def _clr(self): self.db[self._slot]=[]; self._refresh()
    def _exec(self):
        self.db[self._slot]=self._parse(); steps=self.db[self._slot]
        sel=self.hcb.currentText()
        if sel=="Both":
            for i in range(len(self.sessions)): self.execute_action.emit(i,steps)
        else:
            idx=next((i for i,s in enumerate(self.sessions) if s.name==sel),0)
            self.execute_action.emit(idx,steps)
    def _upd_list(self):
        for i in range(40):
            it=self.sl.item(i,1)
            if it: it.setText(str(len(self.db[i])))
    def _save(self):
        p,_=QFileDialog.getSaveFileName(self,"Save Actions","","JSON (*.json)")
        if not p: return
        with open(p,'w') as f: json.dump(self.db,f,indent=2)
        QMessageBox.information(self,"Saved",f"Saved:\n{p}")
    def _load(self):
        p,_=QFileDialog.getOpenFileName(self,"Load Actions","","JSON (*.json)")
        if not p: return
        try:
            with open(p) as f: data=json.load(f)
            for i,s in enumerate(data):
                if i<40: self.db[i]=s
            self._refresh(); self._upd_list()
        except Exception as e: QMessageBox.critical(self,"Error",str(e))

# ──────────────────────────────────────────────────────────────────────────────
#  SETTINGS TAB
# ──────────────────────────────────────────────────────────────────────────────
class SettingsTab(QWidget):
    calib_sig=pyqtSignal(int); clrerr_sig=pyqtSignal(int)
    save_sig=pyqtSignal(int);  reset_sig=pyqtSignal(int)
    dspeed_sig=pyqtSignal(int,list); dforce_sig=pyqtSignal(int,list)

    def __init__(self,sessions:List[HandSession],parent=None):
        super().__init__(parent)
        self.sessions=sessions
        outer=QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)
        scroll=QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        content=QWidget()
        root=QVBoxLayout(content); root.setSpacing(10)
        for i,sess in enumerate(sessions):
            gb=QGroupBox(f"{sess.name} Hand — Device Actions")
            gb.setStyleSheet(f"QGroupBox{{border-color:{sess.accent};}}")
            gl2=QHBoxLayout(gb)
            for lbl,sig,col in [("🔧 Calibrate Force",self.calib_sig,"#238636"),
                                  ("🚫 Clear Errors",self.clrerr_sig,"#9e6a03"),
                                  ("💾 Save to Flash",self.save_sig,"#1f6feb"),
                                  ("⚠ Factory Reset",self.reset_sig,"#da3633")]:
                b=_mk_btn(lbl,col,bold=True)
                b.clicked.connect(lambda _,s=sig,idx=i:s.emit(idx)); gl2.addWidget(b)
            root.addWidget(gb)

        # ── Appearance ──────────────────────────────────────────────────────
        appear_gb = QGroupBox("🎨 Appearance")
        appear_l = QHBoxLayout(appear_gb)
        self.light_mode_cb = QCheckBox("Light Mode")
        self.light_mode_cb.setChecked(LIGHT_MODE)
        self.light_mode_cb.setToolTip(
            "Switches the whole app between the dark theme (default) and a\n"
            "light theme — handy for screenshots on a white background.\n"
            "Takes effect the next time you start the app.")
        self.light_mode_cb.toggled.connect(self._on_light_mode_changed)
        appear_l.addWidget(self.light_mode_cb)
        appear_l.addWidget(QLabel("(takes effect after restarting the app)"))
        appear_l.addStretch()
        root.addWidget(appear_gb)

        # ── Performance / resolution ────────────────────────────────
        perf_gb = QGroupBox("⚡ Performance / Resolution")
        perf_g  = QGridLayout(perf_gb)
        perf_g.addWidget(QLabel("MuJoCo render resolution:"), 0, 0)
        self.render_res_cb = QComboBox()
        for label, w, h in RENDER_PRESETS:
            self.render_res_cb.addItem(label, (w, h))
        _cur = next((i for i,(l,w,h) in enumerate(RENDER_PRESETS)
                     if w == MJ_RENDER_W and h == MJ_RENDER_H), 2)
        self.render_res_cb.setCurrentIndex(_cur)
        self.render_res_cb.setToolTip(
            "Resolution the embedded 3-D hand views are rendered at.\n"
            "Higher = sharper but more GPU/CPU load. Raise this if you\n"
            "have a powerful machine; lower it if the sim feels choppy.")
        self.render_res_cb.currentIndexChanged.connect(self._on_render_res_changed)
        perf_g.addWidget(self.render_res_cb, 0, 1)

        perf_g.addWidget(QLabel("Camera capture resolution:"), 1, 0)
        self.cam_res_cb = QComboBox()
        for label, w, h in CAM_PRESETS:
            self.cam_res_cb.addItem(label, (w, h))
        _curc = next((i for i,(l,w,h) in enumerate(CAM_PRESETS)
                      if w == CAM_CAP_W and h == CAM_CAP_H), 0)
        self.cam_res_cb.setCurrentIndex(_curc)
        self.cam_res_cb.setToolTip(
            "Resolution the webcam is opened at for Motion Capture and\n"
            "Gesture recognition. Higher can improve hand-tracking accuracy\n"
            "at a distance, but costs more CPU per frame.")
        self.cam_res_cb.currentIndexChanged.connect(self._on_cam_res_changed)
        perf_g.addWidget(self.cam_res_cb, 1, 1)

        _pnote = QLabel("Both take effect after restarting the app.")
        _pnote.setStyleSheet(f"color:{PAL['dim']};font-size:{FS_S}pt;")
        perf_g.addWidget(_pnote, 2, 0, 1, 2)
        root.addWidget(perf_gb)

        dp=QGroupBox("Default Power-On Parameters (all hands)")
        dp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        dg=QGridLayout(dp); dg.setColumnStretch(0,1); dg.setColumnStretch(1,2); dg.setColumnStretch(2,2)
        dg.addWidget(QLabel("DOF"),0,0); dg.addWidget(QLabel("Def Speed 0-1000"),0,1); dg.addWidget(QLabel("Def Force 0-3000g"),0,2)
        self._dsp=[]; self._dfo=[]
        for i in range(6):
            dg.addWidget(QLabel(DOF_NAMES[i]),i+1,0)
            sp=QSpinBox(); sp.setRange(0,1000); sp.setValue(500)
            fo=QSpinBox(); fo.setRange(0,3000); fo.setValue(500)
            dg.addWidget(sp,i+1,1); dg.addWidget(fo,i+1,2); self._dsp.append(sp); self._dfo.append(fo)
        ab2=_mk_btn("Apply to All Hands","#1f6feb",bold=True); ab2.clicked.connect(self._apply); dg.addWidget(ab2,7,0,1,3)
        root.addWidget(dp)

        # ── MuJoCo sim tuning (Speed + Smoothing) ──────────────────────────
        sim_gb = QGroupBox("🤖 MuJoCo Sim Tuning  (applies live, all tabs)")
        sim_gl = QGridLayout(sim_gb)
        sim_gl.addWidget(QLabel("Speed:"), 0, 0)
        self.speed_sl = QSlider(Qt.Horizontal); self.speed_sl.setRange(10, 100)
        self.speed_sl.setValue(int(SimTuning.lerp*100))
        self.speed_sl.setToolTip(
            "How fast the simulated hand chases the target pose each frame.\n"
            "Higher = snappier/faster, but can look jumpier on noisy input.")
        sim_gl.addWidget(self.speed_sl, 0, 1)
        self.speed_lbl = QLabel(f"{SimTuning.lerp:.2f}"); self.speed_lbl.setFixedWidth(40)
        sim_gl.addWidget(self.speed_lbl, 0, 2)
        self.speed_sl.valueChanged.connect(self._on_speed_changed)

        sim_gl.addWidget(QLabel("Smoothing:"), 1, 0)
        self.smooth_sl = QSlider(Qt.Horizontal); self.smooth_sl.setRange(5, 100)
        self.smooth_sl.setValue(int(SimTuning.smooth_alpha*100))
        self.smooth_sl.setToolTip(
            "Filters jitter in the INCOMING tracked/streamed pose before it\n"
            "even reaches the sim. Lower = smoother but more lag, "
            "100 = off (raw).")
        sim_gl.addWidget(self.smooth_sl, 1, 1)
        self.smooth_lbl = QLabel(f"{SimTuning.smooth_alpha:.2f}"); self.smooth_lbl.setFixedWidth(40)
        sim_gl.addWidget(self.smooth_lbl, 1, 2)
        self.smooth_sl.valueChanged.connect(self._on_smooth_changed)

        sim_gl.addWidget(QLabel("Force/Tactile Smoothing:"), 2, 0)
        self.fsmooth_sl = QSlider(Qt.Horizontal); self.fsmooth_sl.setRange(2, 50)
        self.fsmooth_sl.setValue(SimTuning.force_approach_step)
        self.fsmooth_sl.setToolTip(
            "As a checked finger nears its own force/tactile limit while\n"
            "closing, its step size is clamped to this many hw-units per\n"
            "move instead of jumping straight past the limit — this is\n"
            "what makes it stop consistently NEAR the configured number\n"
            "instead of overshooting anywhere from ~300 to 1000.\n"
            "Lower = smoother/more precise stop, higher = faster approach.")
        sim_gl.addWidget(self.fsmooth_sl, 2, 1)
        self.fsmooth_lbl = QLabel(str(SimTuning.force_approach_step)); self.fsmooth_lbl.setFixedWidth(40)
        sim_gl.addWidget(self.fsmooth_lbl, 2, 2)
        self.fsmooth_sl.valueChanged.connect(self._on_fsmooth_changed)
        root.addWidget(sim_gb)

        pb=QGroupBox("Polling  (state, force, current AND tactile — all share this interval)")
        pl=QHBoxLayout(pb)
        pl.addWidget(QLabel("Interval (ms):"))
        self.poll_ms=QSpinBox(); self.poll_ms.setRange(20,2000); self.poll_ms.setValue(POLL_MS)
        self.poll_ms.setToolTip(
            "Lower = faster updates for angle/force/current AND tactile sensors\n"
            "(tactile is read every poll cycle, so this directly controls tactile speed too).\n"
            "Too low may overload a slow connection — try 50-80ms if you see errors.")
        self.poll_ms.valueChanged.connect(self._on_poll_ms_changed)
        pl.addWidget(self.poll_ms)
        self.poll_tac=QCheckBox("Poll Tactile Sensors"); self.poll_tac.setChecked(True)
        self.poll_tac.setToolTip("Uncheck to skip tactile reads entirely and poll angle/force only (fastest)")
        self.poll_tac.toggled.connect(self._on_poll_tac_changed)
        pl.addWidget(self.poll_tac); pl.addStretch(); root.addWidget(pb)
        root.addStretch()

        scroll.setWidget(content)
        outer.addWidget(scroll)

    def _on_render_res_changed(self, i):
        w, h = self.render_res_cb.itemData(i)
        try:
            qs = QSettings("InspireHandController", "App")
            qs.setValue("perf/render_w", int(w)); qs.setValue("perf/render_h", int(h)); qs.sync()
        except Exception as e:
            print(f"[perf] save failed: {e}")
        QMessageBox.information(self, "Restart required",
            f"3-D render resolution set to {w}x{h}.\n"
            "It will apply the next time you start the app.")

    def _on_cam_res_changed(self, i):
        w, h = self.cam_res_cb.itemData(i)
        try:
            qs = QSettings("InspireHandController", "App")
            qs.setValue("perf/cam_w", int(w)); qs.setValue("perf/cam_h", int(h)); qs.sync()
        except Exception as e:
            print(f"[perf] save failed: {e}")
        QMessageBox.information(self, "Restart required",
            f"Camera capture resolution set to {w}x{h}.\n"
            "It will apply the next time you start capture.")

    def _on_light_mode_changed(self, checked):
        try:
            qs = QSettings("InspireHandController", "App")
            qs.setValue("ui/light_mode", bool(checked))
            qs.sync()
        except Exception as e:
            print(f"[theme] save failed: {e}")
        QMessageBox.information(self, "Restart required",
            "The new theme will apply the next time you start the app.")

    def _on_speed_changed(self, v):
        SimTuning.lerp = v/100.0
        self.speed_lbl.setText(f"{SimTuning.lerp:.2f}")

    def _on_smooth_changed(self, v):
        SimTuning.smooth_alpha = v/100.0
        self.smooth_lbl.setText(f"{SimTuning.smooth_alpha:.2f}")

    def _on_fsmooth_changed(self, v):
        SimTuning.force_approach_step = v
        self.fsmooth_lbl.setText(str(v))

    def _on_poll_ms_changed(self, v):
        for sess in self.sessions:
            if sess.poller is not None:
                sess.poller.set_interval(v)

    def _on_poll_tac_changed(self, v):
        for sess in self.sessions:
            if sess.poller is not None:
                sess.poller.set_tac(v)

    def _apply(self):
        sp=[s.value() for s in self._dsp]; fo=[f.value() for f in self._dfo]
        for i in range(len(self.sessions)): self.dspeed_sig.emit(i,sp); self.dforce_sig.emit(i,fo)

# ──────────────────────────────────────────────────────────────────────────────
#  LOG TAB
# ──────────────────────────────────────────────────────────────────────────────
class LogTab(QWidget):
    def __init__(self,sessions:List[HandSession],parent=None):
        super().__init__(parent)
        self.sessions=sessions; self._logging=False; self._fh=None; self._wr=None; self._log_tac=False
        root=QVBoxLayout(self)
        ctrl=QHBoxLayout()
        self.start_b=_mk_btn("▶ Start Logging","#238636",bold=True); self.start_b.clicked.connect(self._toggle); ctrl.addWidget(self.start_b)
        ctrl.addWidget(QLabel("File:"))
        self.path=QLineEdit(f"~/inspire_log_{datetime.now():%Y%m%d_%H%M%S}.csv"); ctrl.addWidget(self.path,1)
        br=_mk_btn("📁",PAL['btn_neutral']); br.setFixedWidth(40); br.clicked.connect(self._browse); ctrl.addWidget(br)
        # Off by default: the 1062 tactile values make each row ~25x larger,
        # which only matters to users who need the tactile data.
        self.tac_cb=QCheckBox("Include tactile arrays (1062 values per row)")
        self.tac_cb.setToolTip("Append every tactile sensing point to each logged row. "
                               "Takes effect when logging is started.")
        ctrl.addWidget(self.tac_cb)
        root.addLayout(ctrl)
        self.view=QTextEdit(); self.view.setReadOnly(True); root.addWidget(self.view)
        clr=_mk_btn("🗑 Clear",PAL['btn_neutral']); clr.clicked.connect(self.view.clear); root.addWidget(clr)

    def _browse(self):
        p,_=QFileDialog.getSaveFileName(self,"Save log",self.path.text(),"CSV (*.csv)")
        if p: self.path.setText(p)

    def _toggle(self):
        if not self._logging:
            try:
                self._fh=open(str(Path(self.path.text()).expanduser()),'w',newline='')
                hdr=['timestamp','hand']
                for n in DOF_NAMES: hdr+=[f"ang_{n}",f"frc_{n}",f"cur_{n}",f"tmp_{n}",f"err_{n}"]
                # The choice is fixed for the whole file so every row matches
                # the header; points are flattened row-major per array.
                self._log_tac=self.tac_cb.isChecked()
                if self._log_tac:
                    for _,_a,_b,sh,key in TACTILE_DEFS:
                        hdr+=[f"tac_{key}_{i}" for i in range(sh[0]*sh[1])]
                self._wr=csv.writer(self._fh); self._wr.writerow(hdr)
                self.tac_cb.setEnabled(False)
                self._logging=True; self.start_b.setText("■ Stop Logging")
                self.start_b.setStyleSheet(f"background:#da3633;color:white;border-radius:6px;padding:7px 16px;font-size:{FS}pt;font-weight:bold;")
                self.view.clear()
                self._app("▶ Logging started.")
            except Exception as e: QMessageBox.critical(self,"Error",str(e))
        else:
            self._logging=False
            self.tac_cb.setEnabled(True)
            path = str(Path(self.path.text()).expanduser())
            if self._fh: self._fh.close(); self._fh=None
            self.start_b.setText("▶ Start Logging")
            self.start_b.setStyleSheet(f"background:#238636;color:white;border-radius:6px;padding:7px 16px;font-size:{FS}pt;font-weight:bold;")
            self.view.clear()
            self._app(f"■ Logging finished. Saved to: {path}")
            try:
                size = os.path.getsize(path)
                if size <= 200_000:
                    with open(path, 'r', newline='') as f:
                        self._app(f.read())
                else:
                    self._app(f"(file is {size/1024:.0f} KB — too large to preview here; open it directly)")
            except Exception as e:
                self._app(f"(could not read back file: {e})")

    def feed(self,hand_name,s,td=None):
        if self._logging and self._wr:
            row=[datetime.now().isoformat(),hand_name]
            for i in range(6): row+=[s.get(k,[0]*6)[i] for k in ('angle','force','current','temp','error')]
            if self._log_tac:
                # Empty cells, not zeros, where no frame exists (tactile
                # polling disabled, or a region failed to read), so missing
                # data cannot be mistaken for "no contact".
                for _,_a,_b,sh,key in TACTILE_DEFS:
                    arr=td.get(key) if td else None
                    row+=(arr.ravel().tolist() if arr is not None else ['']*(sh[0]*sh[1]))
            try: self._wr.writerow(row)
            except: pass

    def _app(self,t):
        self.view.append(t)
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

# ──────────────────────────────────────────────────────────────────────────────
#  DUAL CONNECTION PANEL  (wider, scrollable)
# ──────────────────────────────────────────────────────────────────────────────
class DualConnPanel(QWidget):
    connect_req=pyqtSignal(int,str,int,int,bool)
    disconnect_req=pyqtSignal(int)

    def __init__(self,sessions:List[HandSession],parent=None):
        super().__init__(parent)
        self.sessions=sessions; self._fields=[]
        self.setMinimumHeight(72)
        outer=QHBoxLayout(self); outer.setContentsMargins(4,4,4,4); outer.setSpacing(8)
        for i,sess in enumerate(sessions):
            grp=QGroupBox(f"{sess.name} Hand")
            grp.setStyleSheet(
                f"QGroupBox{{border:2px solid {sess.accent};border-radius:6px;}}"
                f"QGroupBox::title{{color:{sess.accent};font-weight:bold;font-size:{FS}pt;}}")
            grp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
   
            GAP_IP, GAP_PORT, GAP_DEV, GAP_GROUP = 10, 10, 10, 40
            gl2=QHBoxLayout(grp); gl2.setSpacing(0); gl2.setContentsMargins(8,14,8,6)
            gl2.addWidget(QLabel("IP:"))
            gl2.addSpacing(GAP_IP)
            ip=QLineEdit(sess.ip); ip.setFixedWidth(130); ip.setFixedHeight(24); gl2.addWidget(ip)
            gl2.addSpacing(GAP_GROUP)
            gl2.addWidget(QLabel("Port:"))
            gl2.addSpacing(GAP_PORT)
            port=QLineEdit(str(HAND_PORT)); port.setFixedWidth(52); port.setFixedHeight(24); gl2.addWidget(port)
            gl2.addSpacing(GAP_GROUP)
            gl2.addWidget(QLabel("Dev ID:"))
            gl2.addSpacing(GAP_DEV)
            dev=QSpinBox(); dev.setRange(1,255); dev.setValue(255); dev.setFixedWidth(55); dev.setFixedHeight(24); gl2.addWidget(dev)
            gl2.addSpacing(GAP_GROUP)
            demo=QCheckBox("Demo"); demo.setChecked(not MB_OK); gl2.addWidget(demo)
            gl2.addSpacing(10)
            cbtn=_mk_btn("🔌 Connect","#238636",bold=True)
            cbtn.setMinimumWidth(110); cbtn.setFixedHeight(28)
            cbtn.clicked.connect(lambda _,idx=i:self._toggle(idx)); gl2.addWidget(cbtn)
            outer.addWidget(grp, 1)
            self._fields.append((ip,port,dev,demo,cbtn))

    def _toggle(self,idx):
        ip,port,dev,demo,_ = self._fields[idx]
        if self.sessions[idx].connected:
            self.disconnect_req.emit(idx)
        else:
            try: p=int(port.text())
            except: p=HAND_PORT
            self.connect_req.emit(idx,ip.text(),p,dev.value(),demo.isChecked())

    def set_state(self,idx,ok,msg=""):
        if idx>=len(self._fields): return
        _,_2,_3,_4,cbtn=self._fields[idx]
        self.sessions[idx].connected=ok
        if ok:
            cbtn.setText("⏹ Disconnect")
            cbtn.setStyleSheet(f"background:#da3633;color:white;border-radius:6px;padding:4px 10px;font-size:{FS}pt;font-weight:bold;")
        else:
            cbtn.setText("🔌 Connect")
            cbtn.setStyleSheet(f"background:#238636;color:white;border-radius:6px;padding:4px 10px;font-size:{FS}pt;font-weight:bold;")

# ──────────────────────────────────────────────────────────────────────────────
#  ACTION EXECUTOR
# ──────────────────────────────────────────────────────────────────────────────
class ActionExec(QThread):
    status=pyqtSignal(str); do_ang=pyqtSignal(list); do_frc=pyqtSignal(list); do_spd=pyqtSignal(list)
    def __init__(self,steps): super().__init__(); self.steps=steps
    def run(self):
        for i,s in enumerate(self.steps):
            self.status.emit(f"Step {i+1}/{len(self.steps)} …")
            self.do_spd.emit([s.get('speed',500)]*6)
            self.do_frc.emit([s.get('force',500)]*6)
            self.do_ang.emit(s.get('angles',[-1]*6))
            self.msleep(s.get('delay',500))
        self.status.emit("Action complete ✓")

# ──────────────────────────────────────────────────────────────────────────────
#  WORKER MANAGER + CALIBRATION  (imported from workers.py / calibration.py)
# ──────────────────────────────────────────────────────────────────────────────
MP_OK   = False
try:
    import mediapipe as _mp_test
    MP_OK = True
except Exception:
    pass

try:
    from workers import WorkerManager, RENDER_W, RENDER_H
    from calibration import (load_range, save_range, load_gestures, save_gesture,
                              delete_gesture, classify, BUILTIN_GESTURES,
                              add_samples, sample_counts, clear_samples, train_model)
    WORKERS_OK = True
except Exception as _we:
    print(f"[workers] {_we}")
    WORKERS_OK = False

# Singleton WorkerManager shared across all tabs
_WORKER_MGR: Optional['WorkerManager'] = None
def get_worker_mgr() -> Optional['WorkerManager']:
    global _WORKER_MGR
    if _WORKER_MGR is None and WORKERS_OK:
        try:
            _WORKER_MGR = WorkerManager()
        except Exception as e:
            print(f"[WorkerManager] {e}")
    return _WORKER_MGR


# ──────────────────────────────────────────────────────────────────────────────
#  NATIVE MUJOCO VIEW WIDGET  (offscreen render → QLabel, driven by QTimer)
# ──────────────────────────────────────────────────────────────────────────────
class SimTuning:
    """Shared, app-wide MuJoCo sim tuning — adjustable live from the Settings
    tab. Every _InspireRenderer/MuJoCoWidget reads these directly, so no
    per-tab wiring is needed; changing a slider in Settings takes effect on
    the very next frame everywhere.
      lerp         — qpos chase speed per frame (0.1=slow/smooth, 1.0=instant)
      smooth_alpha — input EMA smoothing applied in MuJoCoWidget.set_angles
                     before the target ever reaches the renderer
                     (1.0=no smoothing/raw, lower=more smoothing, more lag)

    Thumb/index sequencing (gesture transitions ONLY — never applied to
    slider drags or motion-capture tracking) — used by BOTH the sim's
    _InspireRenderer.set_dofs AND HWSequencer for the real hand:
      seq_deadband — hw-units of slack before a DOF counts as "actually
                     moving" (avoids gate flicker from tiny/noise-level
                     target changes). The actual hold/release rule itself
                     is simple and lives directly in set_dofs()/_tick():
                     closing → thumb waits until the index has FULLY
                     reached its own target; opening → the other 4 fingers
                     wait until the thumb has FULLY reached its target.

    Force/tactile approach smoothing — when a checked finger is closing
    and getting close to its force/tactile limit, steps are clamped so it
    eases in instead of jumping straight past the limit in one move (which
    is what caused the limit to land anywhere from ~300 to 1000 instead of
    consistently near the configured value):
      force_approach_margin — hw-units before the limit where easing-in
                              starts.
      force_approach_step   — max hw-units moved per call once inside that
                              margin. Smaller = smoother/more precise,
                              larger = faster but less precise. This is
                              the "Force/Tactile Smoothing" slider in
                              Settings.
    """
    lerp         = 0.7
    smooth_alpha = 1.0
    thumb_lerp_scale = 0.4   # thumb (DOF 4/5) moves slower than other fingers during
                             # a pose transition, so it doesn't visually punch through
                             # the index finger mid-transition (it arrives LAST instead)
    seq_deadband        = 8
    hwseq_enabled        = False 
    force_approach_margin = 150
    force_approach_step   = 10
   
    tac_dot_scale = 0.50
    tac_dot_max   = 0.004
    tac_inset     = 0.12


# Maps each tactile sensor body in the MJCF to its (region key, grid shape).
# Body names are self-describing: <finger>_force_sensor_<pad|nail|tip>, with
# an extra _mid on the thumb. No left_/right_ prefix in the XML, so this
# table is side-independent.
_TAC_SENSOR_MAP = {
    'pinky_force_sensor_pad': ('pinky_pad',   (10, 8)),
    'pinky_force_sensor_nail': ('pinky_nail',  (12, 8)),
    'pinky_force_sensor_tip': ('pinky_tip',   (3, 3)),
    'ring_force_sensor_pad':   ('ring_pad',    (10, 8)),
    'ring_force_sensor_nail':   ('ring_nail',   (12, 8)),
    'ring_force_sensor_tip':   ('ring_tip',    (3, 3)),
    'middle_force_sensor_pad': ('middle_pad',  (10, 8)),
    'middle_force_sensor_nail': ('middle_nail', (12, 8)),
    'middle_force_sensor_tip': ('middle_tip',  (3, 3)),
    'index_force_sensor_pad':  ('index_pad',   (10, 8)),
    'index_force_sensor_nail':  ('index_nail',  (12, 8)),
    'index_force_sensor_tip':  ('index_tip',   (3, 3)),
    'thumb_force_sensor_pad':  ('thumb_pad',   (12, 8)),
    'thumb_force_sensor_mid':  ('thumb_mid',   (3, 3)),
    'thumb_force_sensor_nail':  ('thumb_nail',  (12, 8)),
    'thumb_force_sensor_tip':  ('thumb_tip',   (3, 3)),
    'palm_force_sensor':     ('palm',        (14, 8)),
}

_TAC_ORIENT = {
    'palm':       {'transpose': True,  'flip_a0': False, 'flip_a1': False},
    'finger':     {'transpose': True,  'flip_a0': True, 'flip_a1': False},
    'thumb_prox': {'transpose': True, 'flip_a0': True,  'flip_a1': False},
    'thumb_dist': {'transpose': True,  'flip_a0': True, 'flip_a1': False},
}

_TAC_SCALE = {
    'palm':        2.0,
    'finger_tip':   1.5,   # pinky/ring/middle/index TIP sensors
    'finger_other': 2,   # pinky/ring/middle/index PAD + NAIL sensors
    'thumb_pad':   4,
    'thumb_mid':   1.5,
    'thumb_nail':  3,
    'thumb_tip':   2.0,
}

def _tac_group(body_name: str) -> str:
    if 'palm' in body_name: return 'palm'
    if 'thumb' in body_name:
        return 'thumb_prox' if body_name in ('thumb_force_sensor_pad', 'thumb_force_sensor_mid') else 'thumb_dist'
    return 'finger'

def _tac_scale_group(tac_key: str) -> str:
    if tac_key == 'palm': return 'palm'
    if tac_key.startswith('thumb_'):
        return tac_key 
    return 'finger_tip' if tac_key.endswith('_tip') else 'finger_other'


def _brighten_scene_for_light_mode(model):

    import mujoco, numpy as np
    try:
        model.vis.headlight.ambient[:]  = [0.55, 0.55, 0.55]
        model.vis.headlight.diffuse[:]  = [0.75, 0.75, 0.75]
        model.vis.rgba.haze[:] = [0.9, 0.9, 0.92, 1.0]
    except Exception as e:
        print(f"[MuJoCo] light-mode headlight/haze tweak skipped: {e}")

    def _recolor_texture(name, light1, light2):
        tex_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_TEXTURE, name)
        if tex_id < 0: return
        w = int(model.tex_width[tex_id]); h = int(model.tex_height[tex_id])
        adr = int(model.tex_adr[tex_id])
        buf = model.tex_data[adr:adr + w*h*3].reshape(h, w, 3)

        orig = buf.astype(np.int32).sum(axis=2)
        mask = orig > orig.mean()
        buf[mask]  = np.array(light1, dtype=np.uint8)
        buf[~mask] = np.array(light2, dtype=np.uint8)

    try:
        _recolor_texture('grid', [206, 212, 218], [173, 181, 189])  # ced4da / adb5bd
        _recolor_texture('sky',  [233, 236, 239], [222, 226, 230])  # e9ecef / dee2e6-ish
    except Exception as e:
        print(f"[MuJoCo] light-mode texture recolor skipped: {e}")


class _InspireRenderer:
    def __init__(self, xml_path: str):
        self.model    = None
        self.data     = None
        self.renderer = None
        self._qadr: Dict[str,int]    = {}   # actuator_name → qpos address
        self._ranges: Dict[str,tuple]  = {}  # actuator_name → (lo, hi) rad
        self._jmap: Dict[str,int]    = {}   # joint_name → qpos address
        self._jrange: Dict[str,tuple]  = {}  # joint_name → (lo, hi) rad
        self._act_joint: Dict[str,str] = {}  # actuator_name → joint_name
        self._dof_master_joint: List[Optional[str]] = [None]*6  # dof_idx → its ACTUATED joint (act_dof{i})
        self._eq_couplings: Dict[str, tuple] = {}  # slave_joint → (master_joint, polycoef[5])
        self._dof_joints: Dict[int,list] = {}  # dof_idx → [joint_name, ...] (fallback: keyword discovery)
        self._targets: Dict[str,float] = {}
        self._side    = 'right'             # overwritten by _load()
        self._lerp    = 0.7
        self._tac_points: Dict[str, list] = {}   # tactile_key -> [(body_id, local_xyz, radius), ...]
        self._tac_data: Dict[str, object] = {}   # latest tactile readings, same dict the tactile tab uses

        # Camera (interactive — drag to rotate/pan, scroll to zoom)
        self.cam           = None

        self._load(xml_path)

    def _load(self, xml_path: str):
        xml_dir = str(Path(xml_path).parent.resolve())
        orig    = os.getcwd()
        os.chdir(xml_dir)
        try:
            import mujoco
            self.model    = mujoco.MjModel.from_xml_path(xml_path)
            if LIGHT_MODE:
                _brighten_scene_for_light_mode(self.model)
            self.data     = mujoco.MjData(self.model)
            mujoco.mj_forward(self.model, self.data)  # populate body positions before first render
            self.renderer = mujoco.Renderer(self.model,
                                            height=MJ_RENDER_H, width=MJ_RENDER_W)
            print(f"[MuJoCo] Loaded: {self.model.nbody} bodies  "
                  f"{self.model.nu} actuators  {self.model.nq} DOF")
        except Exception as e:
            print(f"[MuJoCo] load error: {e}")
            self.model = None
            return
        finally:
            os.chdir(orig)

        # Build actuator → qpos map AND joint → qpos map for fallback
        self._jmap = {}   # reset on each reload
        self._jrange = {}
        for ji in range(self.model.njnt):
            jname = self.model.joint(ji).name
            qadr  = int(self.model.jnt_qposadr[ji])
            self._jmap[jname] = qadr
            self._jrange[jname] = (float(self.model.jnt_range[ji,0]),
                                   float(self.model.jnt_range[ji,1]))

        for i in range(self.model.nu):
            name = self.model.actuator(i).name
            jid  = int(self.model.actuator_trnid[i, 0])
            if jid >= 0:
                jname = self.model.joint(jid).name
                self._qadr[name]      = int(self.model.jnt_qposadr[jid])
                self._ranges[name]    = (float(self.model.actuator_ctrlrange[i,0]),
                                         float(self.model.actuator_ctrlrange[i,1]))
                self._act_joint[name] = jname

        # Preferred path: this app's hardware DOF order (0=pinky,1=ring,
        # 2=middle,3=index,4=thumb-bend,5=thumb-rotate) maps 1:1 onto
        # actuators literally named act_dof0..act_dof5 — confirmed in the
        # generated MJCF. Use that exact mapping when present; it's far
        # more reliable than guessing joint names from keywords.
        self._dof_master_joint = [self._act_joint.get(f'act_dof{i}') for i in range(6)]

        # Read the REAL mechanical coupling for distal/intermediate joints
        # straight from the XML's <equality><joint .../></equality> blocks
        # (e.g. "distal = 0.7 × proximal") instead of independently scaling
        # each joint to its own (much larger) hinge hard-limit — THIS is
        # what was causing distal segments to fold 180° flat onto the
        # proximal link.
        self._eq_couplings = {}
        try:
            for i in range(self.model.neq):
                if int(self.model.eq_type[i]) == int(mujoco.mjtEq.mjEQ_JOINT):
                    j1 = int(self.model.eq_obj1id[i])   # slave / dependent joint
                    j2 = int(self.model.eq_obj2id[i])   # master / independent joint
                    slave_name  = self.model.joint(j1).name
                    master_name = self.model.joint(j2).name
                    poly = [float(x) for x in self.model.eq_data[i][:5]]
                    self._eq_couplings[slave_name] = (master_name, poly)
        except Exception as e:
            print(f"[MuJoCo] equality-constraint parse: {e}")
            self._eq_couplings = {}

        # Detect hand side once from model names
        all_names = (
            [self.model.body(i).name   for i in range(self.model.nbody)] +
            [self.model.actuator(i).name for i in range(self.model.nu)] +
            [self.model.joint(i).name  for i in range(self.model.njnt)]
        )
        self._side = 'left' if any('left' in n for n in all_names) else 'right'

        # Discover EVERY joint belonging to each finger's kinematic chain
        # (proximal + intermediate + distal), not just the first match, so
        # the whole finger curls — not just the palm-to-proximal joint.
        self._discover_dof_joints()

        # Camera
        self.cam           = mujoco.MjvCamera()
        mujoco.mjv_defaultCamera(self.cam)
        self.cam.type      = mujoco.mjtCamera.mjCAMERA_FREE
        self.cam.distance  = 0.35
        self.cam.azimuth   = -90.0
        self.cam.elevation = -5.0
        self.cam.lookat[:] = [0.0, 0.0, 0.12]

        self._build_tactile_geometry()

    def _mesh_local_bbox(self, mesh_name: str):
        """Bounding box (lo, hi) of a mesh's vertices, in MuJoCo's OWN
        compiled mesh-inertial frame (see _build_tactile_geometry for why
        this is NOT the same as the body's local frame)."""
        m = self.model.mesh(mesh_name)
        vadr, vnum = int(m.vertadr[0]), int(m.vertnum[0])
        verts = np.array(self.model.mesh_vert[vadr:vadr+vnum]).reshape(vnum, 3)
        return verts.min(axis=0), verts.max(axis=0)

    def _mesh_frame_to_body(self, mesh_name: str, pts_mesh_frame: np.ndarray) -> np.ndarray:
        """MuJoCo's compiler re-centers/re-orients every mesh into its own
        computed inertial frame, recording the correction as mesh_pos/
        mesh_quat. mesh_vert (and anything computed from it, like our
        bounding box) is expressed in that rotated frame, not in the body's
        local frame. This applies the correction:
        body_local = mesh_pos + mesh_quat * pt."""
        import mujoco
        mid = self.model.mesh(mesh_name).id
        quat = self.model.mesh_quat[mid]
        matp = np.zeros(9)
        mujoco.mju_quat2Mat(matp, quat)
        R = matp.reshape(3, 3)
        return (R @ np.asarray(pts_mesh_frame).T).T + self.model.mesh_pos[mid]

    def _build_tactile_geometry(self):
        """
        For every tactile sensor body defined in the XML, work out exactly
        where each individual cell of its data grid sits ON that sensor's
        OWN mesh surface (using the mesh's real vertex bounding box — no
        guessed dimensions), in the body's LOCAL frame. set_dofs()/render()
        later transforms these into world space every frame using that
        body's CURRENT pose, so the dots track the finger exactly as it
        opens and closes, sitting flush on the surface with no gap.
        """
        self._tac_points = {}
        for body_name, (tac_key, shape) in _TAC_SENSOR_MAP.items():
            try:
                body = self.model.body(body_name)
                bid  = int(body.id)
                mesh_name = body_name + '_mesh'
                lo, hi = self._mesh_local_bbox(mesh_name)   # mesh-inertial frame
                extent = hi - lo
                thin_axis = int(np.argmin(extent))
                grid_axes = [a for a in range(3) if a != thin_axis]
                rows, cols = shape
                # Centered on the thin axis (== "on the surface" for a
                # thin pad mesh, within roughly half its own thickness —
                # a sub-millimetre amount for these sensors, not a visible gap).
                center_thin = 0.5*(lo[thin_axis] + hi[thin_axis])
                # Small inset from the mesh edges so the outermost dots
                # don't hang off the sensor's physical boundary.
                inset = SimTuning.tac_inset
                a0, a1 = grid_axes
                lo0 = lo[a0] + inset*extent[a0]; hi0 = hi[a0] - inset*extent[a0]
                lo1 = lo[a1] + inset*extent[a1]; hi1 = hi[a1] - inset*extent[a1]
                row_step = (hi0 - lo0) / max(1, rows-1) if rows > 1 else 0.0
                col_step = (hi1 - lo1) / max(1, cols-1) if cols > 1 else 0.0
                nonzero_steps = [s for s in (row_step, col_step) if s > 1e-6]
                orient_group = _tac_group(body_name)
                scale_group  = _tac_scale_group(tac_key)
                radius = SimTuning.tac_dot_scale*_TAC_SCALE[scale_group]*min(nonzero_steps) if nonzero_steps else 0.0025
                radius = max(0.0008, min(radius, SimTuning.tac_dot_max))

                orient = _TAC_ORIENT[orient_group]

                mesh_pts = []
                for r in range(rows):
                    fr = r / (rows-1) if rows > 1 else 0.5   # 0..1 across the "row" data axis
                    for c in range(cols):
                        fc = c / (cols-1) if cols > 1 else 0.5   # 0..1 across the "col" data axis
                        # Orientation correction: which fraction drives axis a0
                        # vs a1, and which direction each runs — see _TAC_ORIENT
                        # above if this needs adjusting after real-hand testing.
                        f0, f1 = (fc, fr) if orient['transpose'] else (fr, fc)
                        if orient['flip_a0']: f0 = 1.0 - f0
                        if orient['flip_a1']: f1 = 1.0 - f1
                        v0 = lo0 + f0*(hi0-lo0)
                        v1 = lo1 + f1*(hi1-lo1)
                        local = np.zeros(3)
                        local[a0] = v0; local[a1] = v1; local[thin_axis] = center_thin
                        mesh_pts.append(local)
                # Rotate the whole batch from mesh-inertial frame -> body-local frame
                body_pts = self._mesh_frame_to_body(mesh_name, np.array(mesh_pts))

                pts = []
                k = 0
                for r in range(rows):
                    for c in range(cols):
                        pts.append((bid, body_pts[k], radius, r, c)); k += 1
                self._tac_points[tac_key] = pts
            except Exception as e:
                print(f"[MuJoCo] tactile geometry for {body_name}: {e}")

    def _discover_dof_joints(self):
        """
        Scan EVERY joint in the loaded model and assign it to one of the 6
        hardware DOFs by name keyword. This catches the whole kinematic
        chain of a finger (proximal, intermediate and distal joints) rather
        than only the first, so the whole finger curls together.
        """
        all_jnames = list(self._jmap.keys())
        has_side_prefix = any(('left' in n.lower() or 'right' in n.lower()) for n in all_jnames)
        side = self._side

        finger_kw = {0: ['pinky', 'little'], 1: ['ring'], 2: ['middle'], 3: ['index']}
        rotate_kw = ['aa', 'abduct', 'rotat', 'yaw', 'cmc_aa']

        groups: Dict[int, list] = {i: [] for i in range(6)}
        for jname in all_jnames:
            lname = jname.lower()
            if has_side_prefix and side not in lname:
                continue
            matched = False
            for dof, kws in finger_kw.items():
                if any(kw in lname for kw in kws):
                    groups[dof].append(jname); matched = True; break
            if matched: continue
            if 'thumb' in lname:
                if any(kw in lname for kw in rotate_kw):
                    groups[5].append(jname)
                else:
                    groups[4].append(jname)
        self._dof_joints = groups

    def set_dofs(self, hw_angles: list, sequenced: bool = False, instant: bool = False):
        """
        Convert 6 hardware DOF values (0–1000, 1000=open) to joint angles
        and write directly to qpos + mj_forward (zero physics lag).

        sequenced: only True for an actual discrete GESTURE pose-snap
        (a sudden jump between very different poses, where the thumb could
        visually punch through the index mid-transition). False (default)
        for everything else — live polled hardware state, continuous
        motion-capture tracking, and manual slider drags must always track
        their target immediately with no hold/gate, or they feel broken.

        Layered approach (most → least precise):
          1. Drive ONLY each DOF's actuated master joint (act_dof0..5),
             scaled to ITS OWN mechanical range.
          2. Propagate to every mechanically-coupled slave joint (distal /
             intermediate phalanges) using the REAL polynomial coupling
             ratio read from the XML's <equality> blocks — e.g.
             distal = 0.7 × proximal — instead of independently scaling the
             slave to its own (much larger) hinge hard-limit. This is what
             fixes a distal segment folding 180° flat onto its proximal link.
          3. Fallback: keyword-based multi-joint discovery, for any DOF that
             has no act_dof{i}-named actuator (non-standard XML).
          4. Last resort: old hardcoded actuator/joint name guesses.
        """
        if self.model is None: return
        import mujoco
        S = self._side
        driven: set = set()
        
        # Calculate active lerp speeds
        active_lerp = 1.0 if instant else SimTuning.lerp
        thumb_lerp = 1.0 if instant else (SimTuning.lerp * SimTuning.thumb_lerp_scale)

        def _equiv_hw(dof):
            """Current effective hw-angle (0-1000, 1000=open) for a DOF's
            master joint, derived from its CURRENT qpos (i.e. how far the
            animation has actually progressed, not the raw target)."""
            jname = self._dof_master_joint[dof]
            if jname is None: return None
            qadr = self._jmap.get(jname)
            if qadr is None: return None
            lo, hi = self._jrange.get(jname, (0.0, 1.4381))
            if hi - lo < 1e-6: return None
            cur = float(self.data.qpos[qadr])
            return 1000.0 * (hi - cur) / (hi - lo)

        DEADBAND = SimTuning.seq_deadband
        DONE_TOL = 20
        hold_thumb  = [False, False]   # gates for dof 4 (bend), 5 (rotate)
        hold_finger = [False]*4        # gates for dof 0..3 (pinky/ring/middle/index)
        if sequenced:
            index_eq = _equiv_hw(3)
            thumb_eq = _equiv_hw(4)
            if thumb_eq is not None and hw_angles[4] < thumb_eq - DEADBAND:
                # thumb wants to CLOSE — hold it until index has finished closing
                if index_eq is not None and abs(hw_angles[3] - index_eq) > DONE_TOL:
                    hold_thumb = [True, True]
            elif thumb_eq is not None and hw_angles[4] > thumb_eq + DEADBAND:
                # thumb wants to OPEN — hold the other 4 until thumb has finished opening
                if abs(hw_angles[4] - thumb_eq) > DONE_TOL:
                    hold_finger = [True]*4

        # ── 1) Drive each DOF's actuated master joint ──────────────────────
        for dof in range(6):
            if dof >= 4 and hold_thumb[dof-4]:
                continue   # thumb waits for the index to clear before closing
            if dof < 4 and hold_finger[dof]:
                continue   # finger waits for the thumb to clear before opening
            jname = self._dof_master_joint[dof]
            if jname is None: continue
            qadr = self._jmap.get(jname)
            if qadr is None: continue
            lo, hi = self._jrange.get(jname, (0.0, 1.4381))
            if hi - lo < 1e-6: continue
            frac   = max(0.0, min(1.0, hw_angles[dof] / 1000.0))
            target = lo + (1.0 - frac) * (hi - lo)
            cur    = float(self.data.qpos[qadr])
            self.data.qpos[qadr] = cur + active_lerp * (target - cur)
            driven.add(jname)

        # ── 2) Propagate real mechanical coupling to slave joints ─────────
        # Two passes resolve any 2-level coupling chains (e.g. thumb_2 and
        # thumb_4 both depend on thumb_3, no chaining needed here, but this
        # stays correct even if a future XML chains slave→slave).
        for _ in range(2):
            for slave_name, (master_name, poly) in self._eq_couplings.items():
                s_qadr = self._jmap.get(slave_name)
                m_qadr = self._jmap.get(master_name)
                if s_qadr is None or m_qadr is None: continue
                mv  = float(self.data.qpos[m_qadr])
                val = poly[0] + poly[1]*mv + poly[2]*mv*mv + poly[3]*mv**3 + poly[4]*mv**4
                s_lo, s_hi = self._jrange.get(slave_name, (-1e9, 1e9))
                self.data.qpos[s_qadr] = max(s_lo, min(s_hi, val))
                driven.add(slave_name)

        # ── 3) Fallback: keyword discovery for DOFs with no act_dof match ──
        missing = [d for d in range(6) if self._dof_master_joint[d] is None]
        if missing:
            for dof in missing:
                frac = max(0.0, min(1.0, hw_angles[dof] / 1000.0))
                lerp = active_lerp if dof < 4 else thumb_lerp
                for jname in self._dof_joints.get(dof, []):
                    if jname in driven: continue
                    qadr = self._jmap.get(jname)
                    if qadr is None: continue
                    lo, hi = self._jrange.get(jname, (0.0, 1.4381))
                    if hi - lo < 1e-6: continue
                    target = lo + (1.0 - frac) * (hi - lo)
                    cur    = float(self.data.qpos[qadr])
                    self.data.qpos[qadr] = cur + lerp * (target - cur)
                    driven.add(jname)

        # ── 4) Last resort: old hardcoded names (only if NOTHING was driven)
        if not driven:
            act_map = [
                (0, f'{S}_pinky_MCP_FE_ctrl',  1.4381),
                (1, f'{S}_ring_MCP_FE_ctrl',   1.4381),
                (2, f'{S}_middle_MCP_FE_ctrl', 1.4381),
                (3, f'{S}_index_MCP_FE_ctrl',  1.4381),
                (4, f'{S}_thumb_CMC_FE_ctrl',  0.90),
                (5, f'{S}_thumb_CMC_AA_ctrl',  1.1641),
            ]
            jnt_map = [
                (0, f'{S}_pinky_prox_joint',  1.4381),
                (1, f'{S}_ring_prox_joint',   1.4381),
                (2, f'{S}_middle_prox_joint', 1.4381),
                (3, f'{S}_index_prox_joint',  1.4381),
                (4, f'{S}_thumb_mid_joint',   0.90),
                (5, f'{S}_thumb_base_joint',  1.1641),
            ]
            for dof, act_name, default_max in act_map:
                qadr = self._qadr.get(act_name)
                if qadr is None: continue
                lo, hi = self._ranges.get(act_name, (0.0, default_max))
                frac   = max(0.0, min(1.0, hw_angles[dof] / 1000.0))
                target = lo + (1.0 - frac) * (hi - lo)
                cur    = float(self.data.qpos[qadr])
                lerp = active_lerp if dof < 4 else thumb_lerp
                self.data.qpos[qadr] = cur + lerp * (target - cur)
            for dof, jname, default_max in jnt_map:
                qadr = self._jmap.get(jname)
                if qadr is None: continue
                lo, hi = self._jrange.get(jname, (0.0, default_max))
                frac   = max(0.0, min(1.0, hw_angles[dof] / 1000.0))
                target = lo + (1.0 - frac) * (hi - lo)
                cur    = float(self.data.qpos[qadr])
                lerp = active_lerp if dof < 4 else thumb_lerp
                self.data.qpos[qadr] = cur + lerp * (target - cur)

        mujoco.mj_forward(self.model, self.data)

    def set_tactile(self, tac_data: dict):
        """Latest tactile readings (same dict shape used by the tactile
        tab's FingerTacWidget/PalmTacWidget) — read back in render() to
        color each on-hand sensor dot."""
        self._tac_data = tac_data or {}

    def render(self) -> Optional[np.ndarray]:
        if self.model is None or self.renderer is None: return None
        try:
            self.renderer.update_scene(self.data, camera=self.cam)
            self._inject_tactile_geoms()
            return self.renderer.render()   # (H,W,3) uint8
        except Exception as e:
            print(f"[MuJoCo] render: {e}"); return None

    # Below this raw reading a cell is treated as untouched and not drawn.
    # Matches the alpha cutoff in touch_rgba(), but tested as a plain int so
    # quiet cells cost one comparison instead of a function call.
    TAC_DRAW_MIN = 82          # ~0.02 * 4095
    TAC_MAX_GEOMS = 400        # per-frame budget across all regions

    def _inject_tactile_geoms(self):
        """Draw one small sphere per pressed tactile cell, positioned on its
        sensor's current world transform so the dots track the finger.

        Cost scales with the number of *pressed* cells, not the 1062 total:
        each region is rejected with a single numpy test when nothing on it
        is in contact, and a per-frame budget bounds the worst case."""
        if not self._tac_points or not self._tac_data: return
        import mujoco
        scene = self.renderer.scene
        budget = self.TAC_MAX_GEOMS
        for tac_key, pts in self._tac_points.items():
            arr = self._tac_data.get(tac_key)
            if arr is None: continue
            # one vectorised test skips an untouched pad entirely
            if not (arr >= self.TAC_DRAW_MIN).any(): continue
            for bid, local, radius, r, c in pts:
                if budget <= 0 or scene.ngeom >= scene.maxgeom: return
                try:
                    val = int(arr[r, c])
                except Exception:
                    continue
                if val < self.TAC_DRAW_MIN: continue
                rgba = touch_rgba(val)
                if rgba[3] <= 0.0: continue
                xpos = self.data.xpos[bid]
                xmat = self.data.xmat[bid].reshape(3, 3)
                world = xpos + xmat @ local
                g = scene.geoms[scene.ngeom]
                mujoco.mjv_initGeom(
                    g, type=mujoco.mjtGeom.mjGEOM_SPHERE,
                    size=np.array([radius, 0, 0]),
                    pos=np.array(world, dtype=np.float64),
                    mat=np.eye(3).flatten(),
                    rgba=np.array(rgba, dtype=np.float32))
                scene.ngeom += 1
                budget -= 1

    def rotate(self, daz, del_):
        if self.cam:
            self.cam.azimuth   = (self.cam.azimuth + daz) % 360.0
            self.cam.elevation = float(np.clip(self.cam.elevation + del_, -89, 89))

    def pan(self, dx_px, dy_px):
        """Translate the camera's look-at point along its OWN current
        screen-relative right/up vectors, so dragging right always moves
        the view right regardless of the current rotation."""
        if self.cam is None: return
        az = math.radians(self.cam.azimuth)
        el = math.radians(self.cam.elevation)
        forward = np.array([math.cos(el)*math.cos(az), math.cos(el)*math.sin(az), math.sin(el)])
        world_up = np.array([0.0, 0.0, 1.0])
        right = np.cross(forward, world_up)
        n = float(np.linalg.norm(right))
        right = right/n if n > 1e-6 else np.array([1.0, 0.0, 0.0])
        true_up = np.cross(right, forward)
        scale = 0.0015 * self.cam.distance   # pan speed scales with current zoom
        delta = (-right*dx_px + true_up*dy_px) * scale
        self.cam.lookat[0] = float(self.cam.lookat[0] + delta[0])
        self.cam.lookat[1] = float(self.cam.lookat[1] + delta[1])
        self.cam.lookat[2] = float(self.cam.lookat[2] + delta[2])

    def zoom(self, f):
        if self.cam: self.cam.distance = float(np.clip(self.cam.distance * f, 0.05, 2.0))

    def close(self):
        self.renderer = None; self.model = None



# Motion Capture and Gesture share one camera worker process and one frame
# queue, so only one of them may own the camera at a time. Starting capture
# in either tab stops it in the other, which returns to its stopped state and
# can be started again when brought forward.
_ACTIVE_CAM_TAB = None

def _claim_camera(tab):
    global _ACTIVE_CAM_TAB
    prev = _ACTIVE_CAM_TAB
    if prev is not None and prev is not tab:
        try:
            if getattr(prev, "_active", False):
                prev._stop()
        except RuntimeError:
            pass          # previous tab already destroyed
    _ACTIVE_CAM_TAB = tab

def _release_camera(tab):
    global _ACTIVE_CAM_TAB
    if _ACTIVE_CAM_TAB is tab:
        _ACTIVE_CAM_TAB = None


# One _InspireRenderer per model file, reused for the lifetime of the process.
#
# Freeing a mujoco.Renderer releases its OpenGL context, and on Windows that
# invalidates the contexts still held by every other live renderer -- they
# keep running but draw black frames until recreated. Since several tabs each
# embed a viewer for the same hand, tearing one down on stop would blank the
# others. Renderers are therefore built once per model and kept. Only one
# simulation per hand runs at a time (see _claim_active_sim), so a cached
# renderer is never driven by two views simultaneously.
_RENDERER_CACHE: Dict[str, '_InspireRenderer'] = {}

def _get_renderer(xml_path: str) -> '_InspireRenderer':
    r = _RENDERER_CACHE.get(xml_path)
    if r is None or r.model is None:
        r = _InspireRenderer(xml_path)
        if r.model is not None:
            _RENDERER_CACHE[xml_path] = r
    return r


# At most one live MuJoCo simulation per hand across all tabs: each viewer
# renders on the Qt main thread, so concurrent viewers of the same hand cost
# frame rate. Starting a hand's simulation anywhere stops its simulation
# elsewhere; the other viewer returns to its normal stopped state.
_ACTIVE_MJ_SIM: Dict[str, 'MuJoCoWidget'] = {}

def _claim_active_sim(hand_name: str, widget: 'MuJoCoWidget'):
    prev = _ACTIVE_MJ_SIM.get(hand_name)
    if prev is not None and prev is not widget:
        try:
            if prev._rend is not None:
                prev._stop()
        except RuntimeError:
            pass  # prev widget already destroyed (e.g. its tab was closed)
    _ACTIVE_MJ_SIM[hand_name] = widget


class MuJoCoWidget(QWidget):
    """
    MuJoCo viewer — main-thread QTimer + _InspireRenderer.
    Works on Windows because mujoco was imported before PyQt5.
    Mouse: left-drag=rotate, right-drag=pan, wheel=zoom.
    """
    def __init__(self, hand_name: str, default_xml: str = '', parent=None, auto_start: bool = False):
        super().__init__(parent)
        self._hand = hand_name
        self._xml  = default_xml
        self._rend: Optional[_InspireRenderer] = None
        self._fc   = 0; self._tlast = 0.0
        self._drag = None   # (button, last_x, last_y)
        self._auto_start = auto_start
        self._smoothed: Optional[list] = None   # EMA-smoothed DOF target (SimTuning.smooth_alpha)

        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)

        # XML path is auto-detected next to the app (inspire_hand_left.xml /
        # inspire_hand_right.xml);
        self._path_lbl = QLineEdit(default_xml or '')
        self._path_lbl.setVisible(False)

        self.lbl = QLabel(); self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setToolTip("Left-drag: rotate  ·  Middle-drag: pan  ·  Right-drag/wheel: zoom")
        self.lbl.setMinimumSize(300, 220)
        self.lbl.setStyleSheet(f"background:{PAL['win']};")
        self.lbl.setMouseTracking(True)
        lay.addWidget(self.lbl, 1)   # render view now fills the ENTIRE widget

        # Start button, FPS and status overlaid on the top-left of the render
        # view, so no vertical space is taken from the simulation.
        self._btn = _mk_btn("▶", "#238636", bold=False)
        self._btn.setParent(self.lbl)
        self._btn.setToolTip("Start MuJoCo")
        self._btn.setFixedHeight(16)
        self._btn.setFixedWidth(34)
        self._btn.setStyleSheet(
            "background:#238636;color:white;border-radius:3px;"
            "padding:4px;font-size:7pt;font-weight:bold;")
        self._btn.setEnabled(bool(default_xml and Path(default_xml).exists()))
        self._btn.clicked.connect(self._toggle)

        self._fps = QLabel("—", self.lbl)
        self._fps.setStyleSheet("color:#8b949e;font-size:7pt;background:rgba(13,17,23,160);"
                                "border-radius:3px;padding:1px 4px;")

        self._status = QLabel(
            "" if (default_xml and Path(default_xml).exists()) else "⚠ Hand XML not found next to the app",
            self.lbl)
        self._status.setStyleSheet(f"color:#8b949e;font-size:{FS_S}pt;"
                                   "background:rgba(13,17,23,160);border-radius:3px;padding:1px 4px;")

        # Optional gesture+confidence readout, overlaid top-right of the
        # render view. Hidden unless set_gesture_overlay() is called
        # (only used by the Gesture Recognition tab).
        self._gest_overlay = QLabel(self.lbl)
        self._gest_overlay.setTextFormat(Qt.RichText)
        self._gest_overlay.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self._gest_overlay.setAttribute(Qt.WA_TranslucentBackground, True)
        self._gest_overlay.setAutoFillBackground(False)
        self._gest_overlay.setStyleSheet("background:transparent;border-radius:6px;padding:2px 8px;")
        self._gest_overlay.setVisible(False)

        self._timer = QTimer(self); self._timer.timeout.connect(self._tick)
        self._position_overlay()

        if self._auto_start and self._xml and Path(self._xml).exists():
            QTimer.singleShot(0, self._start)   # after widget is fully constructed

    # ── Mouse events forwarded from label ─────────────────────────────────
    def mousePressEvent(self, ev):
        self._drag = (ev.button(), ev.x(), ev.y())
    def mouseReleaseEvent(self, ev):
        self._drag = None
    def mouseMoveEvent(self, ev):
        if self._drag and self._rend:
            btn, lx, ly = self._drag
            dx, dy = ev.x()-lx, ev.y()-ly
            if btn == Qt.LeftButton:     self._rend.rotate(dx*0.5, dy*0.5)
            elif btn == Qt.RightButton:  self._rend.zoom(1+dy*0.01)
            elif btn == Qt.MiddleButton: self._rend.pan(dx, dy)
            self._drag = (btn, ev.x(), ev.y())
    def wheelEvent(self, ev):
        if self._rend:
            self._rend.zoom(0.9 if ev.angleDelta().y()>0 else 1.1)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._position_overlay()

    # ── Gesture readout overlay (top-right corner of render view) ──────────
    def set_gesture_overlay(self, emoji: str, name: str, conf: int, accent: str = '#58a6ff'):
        html = (f'<div style="background:transparent;text-align:right;line-height:1.2;">'
                f'<span style="font-size:32pt;font-weight:bold;color:{accent};">{emoji}</span>'
                f'<span style="font-size:16pt;font-weight:bold;color:{accent};"> {conf}%</span><br>'
                f'<span style="font-size:16pt;font-weight:bold;color:{accent};">{name}</span>'
                f'</div>')
        self._gest_overlay.setText(html)
        self._gest_overlay.setVisible(True)
        self._gest_overlay.adjustSize()
        self._position_overlay()

    def clear_gesture_overlay(self):
        self._gest_overlay.setText(
            '<div style="background:transparent;text-align:right;">'
            '<span style="font-size:32pt;font-weight:bold;color:#8b949e;">—</span></div>')
        self._gest_overlay.adjustSize()
        self._position_overlay()

    def _position_overlay(self):
        m = 6
        self._btn.move(m, m)
        self._fps.move(m + self._btn.width() + 4, m + 1)
        self._status.adjustSize()
        self._status.move(m, m + self._btn.height() + 4)
        if not self._gest_overlay.isVisible(): return
        x = self.lbl.width() - self._gest_overlay.width() - m
        self._gest_overlay.move(max(0, x), m)


    def _toggle(self):
        if self._rend: self._stop()
        else: self._start()

    def _start(self):
        if not _MJ_OK:
            self._status.setText("⚠ mujoco not installed  (pip install mujoco)"); return
        if not self._xml or not Path(self._xml).exists():
            self._status.setText("⚠ XML not found"); return
        self._status.setText("Loading…"); QApplication.processEvents()
        _claim_active_sim(self._hand, self)
        self._rend = _get_renderer(self._xml)
        if self._rend.model is None:
            self._status.setText("⚠ Load failed — check XML + meshes/ folder")
            self._rend = None; return
        self._timer.start(17)   # ~60 fps
        self._btn.setText("⏹")
        self._btn.setToolTip("Stop MuJoCo")
        self._btn.setStyleSheet(
            "background:#da3633;color:white;border-radius:3px;"
            "padding:4px;font-size:7pt;font-weight:bold;")
        self._status.setText("")
    def _stop(self):
        self._timer.stop()
        # Clear tactile data first: the renderer is cached and outlives this
        # widget, so stale readings would keep being drawn (and keep costing
        # frame time) long after the feed that produced them stopped.
        if self._rend is not None:
            self._rend.set_tactile({})
        # Drop the local reference only; the renderer stays in _RENDERER_CACHE
        # so its GL context is never freed (see _get_renderer).
        self._rend = None
        self._btn.setText("▶")
        self._btn.setToolTip("Start MuJoCo (set XML path in Settings tab)")
        self._btn.setStyleSheet(
            "background:#238636;color:white;border-radius:3px;"
            "padding:4px;font-size:7pt;font-weight:bold;")
        self._status.setText("Stopped"); self.lbl.clear()

    def set_angles(self, a, sequenced: bool = False, instant: bool = False):
        if self._rend is None: return
        
        if instant:
            self._smoothed = list(a)
            self._rend.set_dofs(self._smoothed, sequenced=sequenced, instant=True)
            return
            
        alpha = SimTuning.smooth_alpha
        if alpha >= 0.999 or self._smoothed is None:
            self._smoothed = list(a)
        else:
            self._smoothed = [alpha*v + (1-alpha)*s for v, s in zip(a, self._smoothed)]
        self._rend.set_dofs(self._smoothed, sequenced=sequenced, instant=False)

    def set_tactile(self, tac_data: dict):
        """Forward the latest tactile readings to the renderer so it can
        draw the on-hand sensor-dot overlay (see _InspireRenderer.set_tactile)."""
        if self._rend: self._rend.set_tactile(tac_data)

    def _tick(self):
        if not self._rend: return
        # Skip the work entirely when this view is off screen: on a hidden tab,
        # behind the stacked left/right selector, or with the window minimised.
        # Rendering costs the same whether or not anyone can see the result, and
        # the read-back contends with everything else drawing on the GPU. The
        # timer keeps running, so the view resumes the moment it is shown again.
        if not self.isVisible():
            return
        px = self._rend.render()
        if px is None: return
        try:
            from PyQt5.QtGui import QImage
            h,w,_ = px.shape
            qi = QImage(px.tobytes(),w,h,3*w,QImage.Format_RGB888)
            pm = QPixmap.fromImage(qi).scaled(self.lbl.width(),self.lbl.height(),
                Qt.KeepAspectRatio, Qt.FastTransformation)
            self.lbl.setPixmap(pm)
            self._position_overlay()
            self._fc += 1
            import time; now=time.monotonic()
            if now-self._tlast>=1.0:
                self._fps.setText(f"{self._fc} fps")
                self._fc=0; self._tlast=now
        except Exception: pass

    def cleanup(self): self._stop()


# ──────────────────────────────────────────────────────────────────────────────
#  3-D VIEW TAB  — MuJoCo only (GL skeleton removed)
# ──────────────────────────────────────────────────────────────────────────────
class View3DTab(QWidget):
    def __init__(self, sessions: List[HandSession], parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0)
        _here = Path(__file__).parent

        def _default_xml(sess):
            side = 'right' if sess.name == 'Right' else 'left'
            for c in [_here/f'inspire_hand_{side}.xml',
                      _here/f'rh56dftp_{side}.xml',
                      _here/'rh56dftp_left.xml']:
                if c.exists(): return str(c)
            return ''

        row = QHBoxLayout(); row.setSpacing(6)
        self._mj_widgets: List[MuJoCoWidget] = []
        for sess in sessions:
            gb = QGroupBox(f"🤖 {sess.name} Hand — MuJoCo")
            gb.setStyleSheet(f"QGroupBox{{border-color:{sess.accent};"
                             f"font-weight:bold;font-size:{FS}pt;}}")
            gl2 = QVBoxLayout(gb)
            w = MuJoCoWidget(sess.name, _default_xml(sess))
            self._mj_widgets.append(w); gl2.addWidget(w); row.addWidget(gb)
        lay.addLayout(row)

    def on_state(self, idx: int, s: Dict):
        if idx < len(self._mj_widgets):
            self._mj_widgets[idx].set_angles(s.get('angle', [0]*6))

    def on_tac(self, idx: int, td: Dict):
        if idx < len(self._mj_widgets):
            self._mj_widgets[idx].set_tactile(td)

    def closeEvent(self, ev):
        for w in self._mj_widgets: w.cleanup()
        super().closeEvent(ev)


# ──────────────────────────────────────────────────────────────────────────────
#  CALIBRATION WIZARD DIALOG
# ──────────────────────────────────────────────────────────────────────────────
class CalibrationDialog(QWidget):
    """
    Step-by-step calibration wizard.
    Phase 1: Range calibration — open hand fully, then close completely
    Phase 2: Gesture calibration — show each gesture, record DOF template
    """
    finished = pyqtSignal(dict)   # emits {'range_min':[], 'range_max':[], 'gestures':{}}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Hand Calibration Wizard")
        self.setMinimumSize(500, 400)
        self._dof_buf     = []       # accumulates DOF readings
        self._phase       = 'idle'   # idle / open / close / gesture / ai_gesture
        self._range_min   = [0.0]*6
        self._range_max   = [1.0]*6
        self._gest_names  = list(BUILTIN_GESTURES.keys())
        self._gest_idx    = 0
        self._gest_record = {}
        self._ai_buf      = []       # accumulates frames for the CURRENT AI-training recording
        self._build()

    def _build(self):
        lay = QVBoxLayout(self); lay.setSpacing(10)

        self.title_lbl = QLabel("Hand Calibration")
        self.title_lbl.setStyleSheet(f"font-size:{FS_L+2}pt;font-weight:bold;color:#58a6ff;")
        lay.addWidget(self.title_lbl)

        self.inst_lbl = QLabel("Click a step below to begin.")
        self.inst_lbl.setWordWrap(True)
        self.inst_lbl.setStyleSheet(f"color:{PAL['text']};font-size:{FS_L}pt;padding:8px;")
        lay.addWidget(self.inst_lbl)

        self.prog = QProgressBar(); self.prog.setRange(0,100); self.prog.setValue(0)
        lay.addWidget(self.prog)

        # DOF readout during recording
        self.dof_lbl = QLabel("")
        self.dof_lbl.setAlignment(Qt.AlignCenter)
        self.dof_lbl.setStyleSheet(f"color:{PAL['dim']};font-size:{FS_S}pt;font-family:monospace;")
        lay.addWidget(self.dof_lbl)

        btn_row = QHBoxLayout()
        self.step1_btn = _mk_btn("1️⃣  Open hand (2s)", "#238636", bold=True)
        self.step2_btn = _mk_btn("2️⃣  Close fist (2s)", "#1f6feb", bold=True)
        self.gest_btn  = _mk_btn("3️⃣  Calibrate gestures", "#9e6a03", bold=True)
        self.save_btn  = _mk_btn("💾 Save & Apply", "#238636", bold=True)
        self.save_btn.setEnabled(False)
        self.step1_btn.clicked.connect(self._start_open)
        self.step2_btn.clicked.connect(self._start_close)
        self.gest_btn.clicked.connect(self._start_gesture)
        self.save_btn.clicked.connect(self._save)
        for b in [self.step1_btn, self.step2_btn, self.gest_btn, self.save_btn]:
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        # Gesture list (shows recorded / pending)
        self.gest_table = QTableWidget(len(self._gest_names), 3)
        self.gest_table.setHorizontalHeaderLabels(["Gesture", "Status", "AI Samples"])
        self.gest_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.gest_table.setEditTriggers(QTableWidget.NoEditTriggers)
        for i, name in enumerate(self._gest_names):
            self.gest_table.setItem(i, 0, QTableWidgetItem(name))
            self.gest_table.setItem(i, 1, QTableWidgetItem("pending"))
            self.gest_table.setItem(i, 2, QTableWidgetItem("0"))
        lay.addWidget(self.gest_table)

        # ── AI Gesture Training (optional) ──────────────────────────────
        ai_box = QGroupBox("🤖 AI Gesture Training  (optional — trains a personal model on YOUR recorded samples)")
        ai_lay = QVBoxLayout(ai_box)

        ai_hint = QLabel(
            "Record several short bursts per gesture (vary your hand position slightly each time) — "
            "the more samples, the more robust the trained model. Needs ≥8 samples for at least 2 gestures.")
        ai_hint.setWordWrap(True)
        ai_hint.setStyleSheet(f"color:{PAL['dim']};font-size:{FS_S}pt;")
        ai_lay.addWidget(ai_hint)

        ai_row1 = QHBoxLayout()
        ai_row1.addWidget(QLabel("Gesture:"))
        self.ai_gest_cb = QComboBox(); self.ai_gest_cb.setEditable(True)
        for n in self._gest_names: self.ai_gest_cb.addItem(n)
        self.ai_gest_cb.setToolTip("Pick a built-in gesture, or type a new custom name")
        ai_row1.addWidget(self.ai_gest_cb, 1)
        self.ai_record_btn = _mk_btn("🔴 Record 3s Samples", "#da3633", bold=True)
        self.ai_record_btn.clicked.connect(self._ai_start_record)
        ai_row1.addWidget(self.ai_record_btn)
        ai_lay.addLayout(ai_row1)

        self.ai_counts_lbl = QLabel("No samples recorded yet.")
        self.ai_counts_lbl.setWordWrap(True)
        self.ai_counts_lbl.setStyleSheet(f"color:#58a6ff;font-size:{FS_S}pt;")
        ai_lay.addWidget(self.ai_counts_lbl)

        ai_row2 = QHBoxLayout()
        ai_row2.addWidget(QLabel("Model:"))
        self.ai_model_cb = QComboBox()
        self.ai_model_cb.addItem("KNN (fast, default)", "knn")
        self.ai_model_cb.addItem("Random Forest", "rf")
        self.ai_model_cb.setToolTip(
            "This trains a small scikit-learn classifier on YOUR recorded\n"
            "DOF samples — it is separate from the MediaPipe/ONNX choice in\n"
            "the Camera settings (that's for finding the hand in the video;\n"
            "this is for naming the pose once the hand is found).\n"
            "KNN: fast, works well with few samples per gesture.\n"
            "Random Forest: a bit more robust to noisy/varied samples.")
        ai_row2.addWidget(self.ai_model_cb)
        self.ai_train_btn = _mk_btn("🎯 Train AI Model", "#238636", bold=True)
        self.ai_train_btn.clicked.connect(self._ai_train)
        ai_row2.addWidget(self.ai_train_btn)
        self.ai_clear_btn = _mk_btn("🗑 Clear All Samples", "#9e6a03")
        self.ai_clear_btn.clicked.connect(self._ai_clear)
        ai_row2.addWidget(self.ai_clear_btn)
        ai_lay.addLayout(ai_row2)

        self.ai_status_lbl = QLabel("")
        self.ai_status_lbl.setWordWrap(True)
        self.ai_status_lbl.setStyleSheet(f"color:{PAL['text']};font-size:{FS_S}pt;")
        ai_lay.addWidget(self.ai_status_lbl)

        lay.addWidget(ai_box)
        self._ai_refresh_counts()

        # Timer for recording
        self._rec_timer = QTimer(self)
        self._rec_timer.timeout.connect(self._rec_tick)

    # Called by MotionCaptureTab to feed live DOFs
    def feed_dofs(self, dofs: list):
        if self._phase not in ('open','close','gesture','ai_gesture'):
            return
        self._dof_buf.append(list(dofs))
        self.dof_lbl.setText("DOF: " + "  ".join(f"{v:4d}" for v in dofs))

    def _start_open(self):
        self._phase = 'open'; self._dof_buf = []
        self.inst_lbl.setText("✋  Open your hand as wide as possible — hold for 2 seconds…")
        self.prog.setValue(0)
        self._rec_timer.start(50)   # 50ms ticks, 40 ticks = 2s
        self._tick_count = 0

    def _start_close(self):
        self._phase = 'close'; self._dof_buf = []
        self.inst_lbl.setText("✊  Close your hand into a tight fist — hold for 2 seconds…")
        self.prog.setValue(0)
        self._rec_timer.start(50)
        self._tick_count = 0

    def _start_gesture(self):
        self._gest_idx = 0
        self._next_gesture()

    def _next_gesture(self):
        if self._gest_idx >= len(self._gest_names):
            self.inst_lbl.setText("✅ Gesture calibration complete! Click Save & Apply.")
            self.save_btn.setEnabled(True)
            return
        name = self._gest_names[self._gest_idx]
        self._phase = 'gesture'; self._dof_buf = []
        self.inst_lbl.setText(f"Show gesture:  {name}  — hold for 2 seconds…")
        self.prog.setValue(0)
        self._rec_timer.start(50)
        self._tick_count = 0

    def _rec_tick(self):
        self._tick_count += 1
        target = 60 if self._phase == 'ai_gesture' else 40   # 3s for AI capture, 2s otherwise
        pct = min(100, int(self._tick_count / target * 100))
        self.prog.setValue(pct)
        if self._tick_count < target:
            return
        self._rec_timer.stop()

        if self._phase == 'open':
            if self._dof_buf:
                arr = np.array(self._dof_buf)
                # DOFs are now 1000=open; convert to curl domain (0=open) for lm_to_dof
                self._range_min = (1.0 - arr.mean(0)/1000.).tolist()
            self.inst_lbl.setText("✅ Open position recorded. Now close your fist.")
            self.prog.setValue(100)
            self._phase = 'idle'

        elif self._phase == 'close':
            if self._dof_buf:
                arr = np.array(self._dof_buf)
                # DOFs are now 0=closed; convert to curl domain (1=closed) for lm_to_dof
                self._range_max = (1.0 - arr.mean(0)/1000.).tolist()
            self.inst_lbl.setText("✅ Closed position recorded. Calibrate gestures next.")
            self.save_btn.setEnabled(True)
            self.prog.setValue(100)
            self._phase = 'idle'

        elif self._phase == 'gesture':
            name = self._gest_names[self._gest_idx]
            if self._dof_buf:
                arr = np.array(self._dof_buf)
                mean_dofs = arr.mean(0).astype(int).tolist()
                self._gest_record[name] = mean_dofs
                it = self.gest_table.item(self._gest_idx, 1)
                if it: it.setText(f"✅ {mean_dofs[:4]}…")
            self._gest_idx += 1
            self._phase = 'idle'
            self._next_gesture()

        elif self._phase == 'ai_gesture':
            label = self.ai_gest_cb.currentText().strip()
            n = len(self._dof_buf)
            if self._dof_buf and label:
                add_samples(label, self._dof_buf)
                self.ai_status_lbl.setText(f"✅ Recorded {n} samples for \"{label}\".")
            else:
                self.ai_status_lbl.setText("⚠ No frames captured — make sure Motion Capture is running.")
            self._phase = 'idle'
            self.ai_record_btn.setEnabled(True)
            self._ai_refresh_counts()

    # ── AI Gesture Training ─────────────────────────────────────────────
    def _ai_start_record(self):
        label = self.ai_gest_cb.currentText().strip()
        if not label:
            self.ai_status_lbl.setText("⚠ Type or pick a gesture name first."); return
        if self._phase != 'idle':
            self.ai_status_lbl.setText("⚠ Another recording is already in progress."); return
        self._phase = 'ai_gesture'; self._dof_buf = []
        self.inst_lbl.setText(f"🔴 Recording samples for \"{label}\" — hold the pose for 3 seconds…")
        self.ai_status_lbl.setText("Recording…")
        self.prog.setValue(0)
        self.ai_record_btn.setEnabled(False)
        self._rec_timer.start(50)
        self._tick_count = 0

    def _ai_refresh_counts(self):
        counts = sample_counts()
        if not counts:
            self.ai_counts_lbl.setText("No samples recorded yet.")
        else:
            self.ai_counts_lbl.setText("Samples so far:  " +
                "   ".join(f"{k}: {v}" for k, v in counts.items()))
        for i, name in enumerate(self._gest_names):
            it = self.gest_table.item(i, 2)
            if it: it.setText(str(counts.get(name, 0)))

    def _ai_train(self):
        model_type = self.ai_model_cb.currentData()
        ok, msg, acc = train_model(model_type=model_type)
        if ok:
            acc_txt = f"  (cross-val accuracy ≈ {acc*100:.0f}%)" if acc is not None else ""
            self.ai_status_lbl.setText(f"🎯 {msg}{acc_txt}\nThe app will use this model automatically.")
        else:
            self.ai_status_lbl.setText(f"⚠ {msg}")

    def _ai_clear(self):
        reply = QMessageBox.question(self, "Clear AI Samples",
            "This deletes ALL recorded AI-training samples (not the trained model itself, "
            "and not your built-in/personal gesture templates). Continue?",
            QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            clear_samples()
            self._ai_refresh_counts()
            self.ai_status_lbl.setText("🗑 All samples cleared.")

    def _save(self):
        save_range(self._range_min, self._range_max)
        for name, dofs in self._gest_record.items():
            save_gesture(name, dofs)
        self.finished.emit({
            'range_min':  self._range_min,
            'range_max':  self._range_max,
            'gestures':   self._gest_record,
        })
        self.inst_lbl.setText("✅ Calibration saved! Range and gestures updated.")


# ──────────────────────────────────────────────────────────────────────────────
#  MOTION CAPTURE TAB  (multiprocessing CameraProc)
# ──────────────────────────────────────────────────────────────────────────────
class MotionCaptureTab(QWidget):
    """Live motion capture: camera feed mirrored to MuJoCo sim in real time.
    The camera|sim layout lets you see the hand actually move.
    Raw DOF angles optionally forwarded to real hardware via the → Hands checkbox."""
    send_angles = pyqtSignal(int, list)

    def __init__(self, sessions, parent=None):
        super().__init__(parent)
        self.sessions    = sessions
        self._active     = False
        self._mgr        = None
        self._cal_win    = None
        self._cal        = load_range()
        self._poll_timer = QTimer(self); self._poll_timer.timeout.connect(self._poll)
        self._build()

    def _build(self):
        root = QVBoxLayout(self); root.setSpacing(4)

        # ── Controls row ────────────────────────────────────────────────
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel('Camera:'))
        self.cam_cb = QComboBox()
        _cam_list = []
        try:
            from workers import enumerate_cameras as _ecam; _cam_list = _ecam()
        except Exception: pass
        if not _cam_list: _cam_list = [(i, f'Camera {i}') for i in range(4)]
        self._cam_indices = [idx for idx,_ in _cam_list]
        for _, name in _cam_list: self.cam_cb.addItem(name)
        self.cam_cb.setFixedWidth(95); ctrl.addWidget(self.cam_cb)

        ctrl.addWidget(QLabel('Mode: MediaPipe'))

        self.start_btn = _mk_btn('▶ Start Capture', '#238636', bold=True)
        self.start_btn.setEnabled(MP_OK)
        self.start_btn.clicked.connect(self._toggle); ctrl.addWidget(self.start_btn)

        self.mirror_cb = QCheckBox('Mirror'); self.mirror_cb.setChecked(True); ctrl.addWidget(self.mirror_cb)
        self.send_cb   = QCheckBox('→ Hands'); self.send_cb.setChecked(True); ctrl.addWidget(self.send_cb)

        ctrl.addWidget(QLabel('Smooth:'))
        self.smooth_sb = QSpinBox(); self.smooth_sb.setRange(1,15); self.smooth_sb.setValue(4)
        ctrl.addWidget(self.smooth_sb)
        self.cal_btn = _mk_btn('🎯 Calibrate', '#9e6a03', bold=True)
        self.cal_btn.clicked.connect(self._open_calibration); ctrl.addWidget(self.cal_btn)
        ctrl.addStretch(); root.addLayout(ctrl)

        # ── Main area: camera | MuJoCo sim ────────────────────────────────
        split = QSplitter(Qt.Horizontal); split.setHandleWidth(5)

        cam_box = QGroupBox('Camera Feed')
        cl = QVBoxLayout(cam_box)
        self.cam_lbl = QLabel('Capture not started')
        self.cam_lbl.setAlignment(Qt.AlignCenter)
        self.cam_lbl.setMinimumSize(380, 240)
        self.cam_lbl.setStyleSheet(f'background:{PAL["panel"]};color:{PAL["dim"]};')
        cl.addWidget(self.cam_lbl)
        split.addWidget(cam_box)

        mj_box = QGroupBox('🤖 Live MuJoCo  ·  mirrors detected hand motion in real time')
        ml = QVBoxLayout(mj_box)
        self._mj_stack = QStackedWidget()
        _here = Path(__file__).parent
        self._mocap_mj = []
        for sess in self.sessions:
            side = 'right' if sess.name=='Right' else 'left'
            _def = next((str(p) for p in [
                _here/f'inspire_hand_{side}.xml',
                _here/f'rh56dftp_{side}.xml'] if p.exists()),'')
            w = MuJoCoWidget(sess.name, _def)
            w._btn.setVisible(False); w._status.setVisible(False)  # auto-started/stopped with capture below
            self._mocap_mj.append(w); self._mj_stack.addWidget(w)

        _right_idx = next((i for i,s in enumerate(self.sessions) if s.name=='Right'), 0)
        self._mj_stack.setCurrentIndex(_right_idx)
        ml.addWidget(self._mj_stack)
        split.addWidget(mj_box)
        split.setSizes([480, 480])
        root.addWidget(split, 1)

    def _toggle(self):
        if not self._active: self._start()
        else: self._stop()

    def _start(self):
        self._mgr = get_worker_mgr()
        if not self._mgr: return
        here = Path(__file__).parent
        cal  = load_range()
        cfg  = {
            'cam_idx':         self._cam_indices[self.cam_cb.currentIndex()] if self._cam_indices else 0,
            'mode':            'mediapipe',
            'max_hands':       2,
            'cam_w':           CAM_CAP_W,
            'cam_h':           CAM_CAP_H,
            'smooth_k':        self.smooth_sb.value(),
            'mirror':          self.mirror_cb.isChecked(),
            'range_min':       cal.get('range_min',[0.0]*6),
            'range_max':       cal.get('range_max',[1.0]*6),
            'personal_gestures': None,
            'detect_gestures': False,
        }
        _claim_camera(self)
        self._mgr.stop_camera(); self._mgr.start_camera(cfg)
        self._poll_timer.start(33)
        self._active = True
        for w in self._mocap_mj:
            if w._rend is None: w._start()
        self.start_btn.setText('⏹ Stop Capture')
        self.start_btn.setStyleSheet(
            f'background:#da3633;color:white;border-radius:6px;'
            f'padding:4px 10px;font-size:{FS}pt;font-weight:bold;')

    def _stop(self):
        self._poll_timer.stop()
        _release_camera(self)
        if self._mgr: self._mgr.stop_camera()
        self._active = False
        for w in self._mocap_mj:
            if w._rend is not None: w._stop()
        self.start_btn.setText('▶ Start Capture')
        self.start_btn.setStyleSheet(
            f'background:#238636;color:white;border-radius:6px;'
            f'padding:4px 10px;font-size:{FS}pt;font-weight:bold;')
        self.cam_lbl.setText('Stopped')

    def _poll(self):
        if not self._mgr: return
        def _idx_for(side):
            return next((i for i,s in enumerate(self.sessions) if s.name == side), None)
        while not self._mgr.cam_frame_q.empty():
            try:
                jpg = self._mgr.cam_frame_q.get_nowait()
                import numpy as np_; import cv2 as cv_
                arr = np_.frombuffer(jpg, dtype=np_.uint8)
                frame = cv_.imdecode(arr, cv_.IMREAD_COLOR)
                if frame is not None:
                    h,w,ch = frame.shape
                    from PyQt5.QtGui import QImage as QI_
                    qi = QI_(frame.data,w,h,ch*w,QI_.Format_BGR888)
                    pm = QPixmap.fromImage(qi).scaled(
                        self.cam_lbl.width(), self.cam_lbl.height(),
                        Qt.KeepAspectRatio, Qt.FastTransformation)
                    self.cam_lbl.setPixmap(pm)
            except Exception: pass

        while not self._mgr.cam_out_q.empty():
            try:
                msg = self._mgr.cam_out_q.get_nowait()
                for det in msg.get('hands',[]):
                    side  = det['hand']; dofs = det['dof']
                    s_idx = _idx_for(side)
                    if s_idx is None: continue
                    if s_idx < len(self._mocap_mj):
                        self._mj_stack.setCurrentIndex(s_idx)
                        self._mocap_mj[s_idx].set_angles(dofs)
                    if self.send_cb.isChecked():
                        self.send_angles.emit(s_idx, dofs)
                    if self._cal_win: self._cal_win.feed_dofs(dofs)
            except Exception: pass

    def _open_calibration(self):
        if self._cal_win is None or not self._cal_win.isVisible():
            self._cal_win = CalibrationDialog()
            self._cal_win.finished.connect(self._on_cal_done)
        self._cal_win.show(); self._cal_win.raise_()

    def _on_cal_done(self, result):
        self._cal = result
        QMessageBox.information(self, 'Calibration saved', 'Range calibration saved.\nRestart capture to apply.')

    def cleanup(self): self._stop()


# ──────────────────────────────────────────────────────────────────────────────
class GestureRecognitionTab(QWidget):
    send_angles = pyqtSignal(int, list)

    def __init__(self, sessions: List[HandSession], parent=None):
        super().__init__(parent)
        self.sessions  = sessions
        self._active   = False
        self._mgr: Optional[WorkerManager] = None
        self._poll_timer = QTimer(self); self._poll_timer.timeout.connect(self._poll)
        self._hold_ctr = [0]*len(sessions)
        self._prev_det = [""]*len(sessions)
        self._templates = load_gestures()
        self._build()

    @staticmethod
    def _split_gesture(text: str):
        """Splits '✊ Fist' into ('✊', 'Fist') on the first space."""
        if not text: return '—', ''
        parts = text.split(' ', 1)
        return (parts[0], parts[1]) if len(parts) == 2 else (text, '')

    def _build(self):
        root = QVBoxLayout(self); root.setSpacing(4)
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Camera:"))
        self.cam_cb = QComboBox()
        _cam_list = []
        try:
            from workers import enumerate_cameras as _ecam
            _cam_list = _ecam()
        except Exception:
            pass
        if not _cam_list:
            _cam_list = [(i, f"Camera {i}") for i in range(4)]
        self._cam_indices = [idx for idx,_ in _cam_list]
        for _, name in _cam_list: self.cam_cb.addItem(name)
        self.cam_cb.setFixedWidth(95); ctrl.addWidget(self.cam_cb)

        ctrl.addWidget(QLabel("Mode: MediaPipe"))

        self.start_btn = _mk_btn("▶ Start Recognition", "#238636", bold=True)
        self.start_btn.setEnabled(MP_OK)
        self.start_btn.clicked.connect(self._toggle)
        ctrl.addWidget(self.start_btn)

        ctrl.addWidget(QLabel("Hold:"))
        self.hold_sb = QSpinBox(); self.hold_sb.setRange(1,20); self.hold_sb.setValue(5)
        self.hold_sb.setToolTip("Frames a gesture must hold steady before it's auto-sent to the hand")
        ctrl.addWidget(self.hold_sb)
        ctrl.addStretch(); root.addLayout(ctrl)

        split = QSplitter(Qt.Horizontal); split.setHandleWidth(5)

        # Camera feed
        cam_box = QGroupBox("Camera Feed")
        cl = QVBoxLayout(cam_box)
        self.cam_lbl = QLabel("Recognition not started")
        self.cam_lbl.setAlignment(Qt.AlignCenter)
        self.cam_lbl.setMinimumSize(380, 240)
        self.cam_lbl.setStyleSheet(f"background:{PAL['panel']};color:{PAL['dim']};")
        cl.addWidget(self.cam_lbl)
        split.addWidget(cam_box)

        # MuJoCo sim panels - one per hand, stacked (fast switch on detection).
        # Detected gesture + confidence is overlaid top-right of this view
        # (see MuJoCoWidget.set_gesture_overlay) - sent to the hand immediately,
        # no separate template window or Send button needed.
        mj_box = QGroupBox("🤖 MuJoCo Sim  ·  gesture + confidence shown top-right ·  auto-sent to hand")
        ml = QVBoxLayout(mj_box)
        self._mj_stack = QStackedWidget()
        _here = Path(__file__).parent
        self._gest_mj: List[MuJoCoWidget] = []
        for sess in self.sessions:
            side = 'right' if sess.name=='Right' else 'left'
            _def = next((str(p) for p in [
                _here/f'inspire_hand_{side}.xml',
                _here/f'rh56dftp_{side}.xml'] if p.exists()),'')
            w = MuJoCoWidget(sess.name, _def)
            w._btn.setVisible(False); w._status.setVisible(False)  # auto-started/stopped with recognition below
            self._gest_mj.append(w)
            self._mj_stack.addWidget(w)
        _right_idx = next((i for i,s in enumerate(self.sessions) if s.name=='Right'), 0)
        self._mj_stack.setCurrentIndex(_right_idx)
        ml.addWidget(self._mj_stack)
        split.addWidget(mj_box)
        split.setSizes([420, 420])
        root.addWidget(split, 1)

    def _toggle(self):
        if not self._active: self._start()
        else: self._stop()

    def _start(self):
        self._mgr = get_worker_mgr()
        if not self._mgr: return
        here = Path(__file__).parent
        cal  = load_range()
        cfg  = {
            'cam_idx':         self._cam_indices[self.cam_cb.currentIndex()] if hasattr(self,'_cam_indices') and self._cam_indices else self.cam_cb.currentIndex(),
            'mode':            'mediapipe',
            'max_hands':       2,
            'cam_w':           CAM_CAP_W,
            'cam_h':           CAM_CAP_H,
            'smooth_k':        4,
            'mirror':          True,
            'range_min':       cal.get('range_min', [0.0]*6),
            'range_max':       cal.get('range_max', [1.0]*6),
            'personal_gestures': {k:v for k,v in load_gestures().items()
                                  if k not in BUILTIN_GESTURES},
            'detect_gestures': True,
        }
        _claim_camera(self)
        self._mgr.stop_camera()
        self._mgr.start_camera(cfg)
        self._poll_timer.start(33)
        self._active = True
        for w in self._gest_mj:
            if w._rend is None: w._start()
        self.start_btn.setText("⏹ Stop")
        self.start_btn.setStyleSheet(
            f"background:#da3633;color:white;border-radius:6px;"
            f"padding:4px 10px;font-size:{FS}pt;font-weight:bold;")

    def _stop(self):
        self._poll_timer.stop()
        _release_camera(self)
        if self._mgr: self._mgr.stop_camera()
        self._active = False
        for w in self._gest_mj:
            if w._rend is not None: w._stop()
        self.start_btn.setText("▶ Start Recognition")
        self.start_btn.setStyleSheet(
            f"background:#238636;color:white;border-radius:6px;"
            f"padding:4px 10px;font-size:{FS}pt;font-weight:bold;")
        self.cam_lbl.setText("Stopped")
        for w in self._gest_mj: w.clear_gesture_overlay()

    def _poll(self):
        if not self._mgr: return
        def _idx_for(side):
            return next((i for i,s in enumerate(self.sessions) if s.name == side), None)

        while not self._mgr.cam_frame_q.empty():
            try:
                jpg = self._mgr.cam_frame_q.get_nowait()
                import numpy as np_; import cv2 as cv_
                arr = np_.frombuffer(jpg, dtype=np_.uint8)
                frame = cv_.imdecode(arr, cv_.IMREAD_COLOR)
                if frame is not None:
                    h,w,ch = frame.shape
                    from PyQt5.QtGui import QImage as QI_
                    qi = QI_(frame.data, w, h, ch*w, QI_.Format_BGR888)
                    pm = QPixmap.fromImage(qi).scaled(
                        self.cam_lbl.width(), self.cam_lbl.height(),
                        Qt.KeepAspectRatio, Qt.FastTransformation)
                    self.cam_lbl.setPixmap(pm)
            except Exception:
                pass

        while not self._mgr.cam_out_q.empty():
            try:
                msg = self._mgr.cam_out_q.get_nowait()
                for det in msg.get('hands', []):
                    side  = det['hand']
                    gest  = det.get('gest', '')
                    conf  = det.get('conf', 0)
                    s_idx = _idx_for(side)
                    if s_idx is None: continue

                    # ── Overlay: show detected gesture name+confidence ──────
                    if s_idx < len(self._gest_mj):
                        self._mj_stack.setCurrentIndex(s_idx)
                        if gest and gest != 'Unknown':
                            emoji, name = self._split_gesture(gest)
                            accent = self.sessions[s_idx].accent if s_idx < len(self.sessions) else '#58a6ff'
                            self._gest_mj[s_idx].set_gesture_overlay(emoji, name, conf, accent)
                            # Show TEMPLATE pose in the sim immediately
                            tmpl = self._templates.get(gest)
                            if tmpl:
                                self._gest_mj[s_idx].set_angles(list(tmpl), sequenced=True)
                        else:
                            self._gest_mj[s_idx].clear_gesture_overlay()

                    # ── Hold confirmation then send template to real hand ───
                    if gest == self._prev_det[s_idx]:
                        self._hold_ctr[s_idx] += 1
                    else:
                        self._hold_ctr[s_idx] = 0
                        self._prev_det[s_idx] = gest
                    if (self._hold_ctr[s_idx] >= self.hold_sb.value() and
                            gest not in ('', 'Unknown')):
                        tmpl = self._templates.get(gest)
                        if tmpl:
                            self.send_angles.emit(s_idx, list(tmpl))
                            self._hold_ctr[s_idx] = 0
            except Exception:
                pass

    def cleanup(self):
        self._stop()


class HelpTab(QWidget):
    """in-app user guide -- what each tab does and how the
    pieces fit together, so a new user (or a reviewer) doesn't need the
    README open side-by-side just to find their way around."""
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self); outer.setContentsMargins(8,8,8,8)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setStyleSheet(
            f"QTextBrowser{{background:{PAL['panel']};color:{PAL['text']};"
            f"border:1px solid {PAL['border']};border-radius:6px;"
            f"font-size:{FS}pt;padding:6px;}}")
        browser.setHtml(f"""
        <h2 style="color:#58a6ff;">{APP_NAME} — Help</h2>
        <p>This application controls, calibrates, simulates, and teleoperates one or
        two Inspire Hand RH56 series specialy the RH56DFTP dexterous hands. Use the tabs across the top of the
        window to move between the tools described below. Every tab (except live
        device polling) works with <b>no physical hand connected</b>, so the whole
        tool doubles as a self-contained way to learn or teach dexterous-hand
        concepts.</p>

        <h3>📊 Dashboard</h3>
        <p>Live overview of each connected hand: per-DOF angle, force, current, and
        temperature readings, plus a “Launch MuJoCo 3-D” button for a quick kinematic
        preview. The preview works even with no physical hand connected, and its
        pose updates automatically from whatever is driving that hand (real
        hardware, Demo mode, or the Control tab's Simulation-only mode below).</p>

        <h3>Network setup</h3>
        <p>The hands are reached over Ethernet, so this PC must sit on the
        <b>same subnet</b> as the hand. Give the network interface a static
        address in the same range as the hands, with subnet mask
        <b>255.255.255.0</b>. For example, with hands at 192.168.11.210 and
        192.168.11.211, set the PC to 192.168.11.100 / 255.255.255.0. A PC
        left on DHCP, or on a different range, will not reach the hands no
        matter what is entered in the Settings tab. Two hands must have
        different addresses on that subnet.</p>

        <h3>Calibration files are examples only</h3>
        <p>The calibration and gesture-model files shipped with the app come
        from the authors' own setup and exist so that it starts in a working
        state. They were recorded with one camera, one pair of hands and one
        lighting arrangement, and the mapping from camera landmarks to finger
        closure depends on all three, so they may work poorly elsewhere.
        Before relying on motion capture or gestures, redo the range
        calibration and collect and train your own gesture samples in your own
        environment.</p>

        <h3>Reading and resizing the views</h3>
        <p>The live plots are interactive. Scroll the <b>mouse wheel</b> over a
        plot to zoom both axes; hold the <b>right mouse button</b> and drag to
        stretch or compress a single axis (drag sideways for time, up and down
        for the value axis); hold the <b>left mouse button</b> to pan. The small
        <b>A</b> button in the bottom-left corner of a plot restores automatic
        scaling. The time axis counts seconds since the current connection was
        established, so it restarts at zero each time you connect.</p>
        <p>Every panel in the application can be resized: drag the divider bars
        between the plot, hand-view and tactile areas to give more room to
        whichever you are watching, and the window itself can be resized or
        maximised as usual. Panel sizes are remembered between sessions.</p>

        <h3>🖐 Tactile Map</h3>
        <p>Real-time heatmap of the palm and fingertip tactile sensors, one tab per
        connected hand. Each hand carries 17 separate arrays — a fingertip
        (3x3), nail (12x8) and pad (10x8) region on each finger, a mid-phalanx
        region on the thumb, and a 14x8 palm array — giving 1062 individual
        sensing points, each read as a 12-bit value from 0 (no contact) to
        4095 (hard press). Colors range from blue through cyan, green and
        yellow to red; an unpressed cell blends into the panel background so
        the grid reads as blank until something registers force. Drag the
        splitter handles between the finger and palm panels to resize them —
        the palm panel starts a bit wider than the fingers by default.</p>

        <h3>🎮 3-D View</h3>
        <p>A dedicated, larger MuJoCo simulation view per hand, for when you want
        the kinematic preview to be the main focus rather than a small panel.
        Left-drag to rotate, middle-drag to pan, right-drag or scroll wheel to
        zoom. Click ▶ to start the simulation and ⏹ to stop it.</p>
        <p><b>Note:</b> only one simulation per hand runs at a time, even across
        different tabs — starting a simulation for a hand in one tab automatically
        stops that same hand's simulation elsewhere, to keep the frame rate smooth.
        The other tab's viewer isn't broken, it's just paused — click ▶ there again
        whenever you want to switch back to it. The Motion Capture and Gesture tabs
        manage their own simulations automatically and don't need this click at
        all (see below).</p>

        <h3>🎮 Control</h3>
        <p>Manual control per hand: sliders for each of the six actuated degrees of
        freedom, quick preset-gesture buttons (Open, Fist, Point, OK, Thumbs Up,
        Rock), and a “Both” tab for sending the same preset to both hands at once.
        Also includes per-DOF force limits and tactile-triggered thresholds.</p>
        <p><b>🎓 Simulation-only mode:</b> tick this checkbox above a hand's embedded
        3-D preview to drive that preview directly from the sliders/presets, with
        nothing sent to the real hand or Demo generator — useful for learning the
        hand's kinematics or demonstrating the software with no hardware at all.
        Untick it to go back to normal control; normal (unticked) behavior is
        completely unchanged from before this option existed.</p>

        <h3>📜 Actions</h3>
        <p>Build and run short scripted sequences: up to 40 slots, each specifying a
        target pose for all six DOF plus speed, force, and a delay before the next
        step — useful for repeatable demonstrations or bench tests.</p>

        <h3>📷 Motion Capture</h3>
        <p>Drive the hand(s) from your webcam using MediaPipe hand tracking. Click
        ▶ Start Capture and show your hand(s) to the camera — whichever hand
        MediaPipe reports (Left/Right, from the camera's point of view) is matched
        automatically to the connected session of the same name and its simulation
        starts and updates by itself; there's nothing to start or assign manually.
        Tick “→ Hands” to also send the tracked motion to the physical hardware
        (leave it unticked to preview in simulation only). Stopping capture stops
        both hands' simulations too.</p>

        <p><b>Which hand moves:</b> left and right are detected automatically
        from the camera, and the match follows the hand you show, not the hand
        that is plugged in. If you show your left hand while only the right
        hand is connected, the 3-D view correctly shows the left model
        following you, but no physical hand moves, because no device is bound
        to that side. Show the hand that matches a connected session.</p>
        <p><b>Accuracy:</b> MediaPipe estimates landmarks from a single
        uncalibrated camera with no depth information, and those 21 points are
        reduced to six coupled closure values. It is responsive and needs no
        extra equipment, but it is not an exact reproduction of your hand
        pose. Occlusion, palm rotation away from the camera and poor lighting
        all degrade it. For precise joint reproduction an instrumented glove
        or marker-based motion capture remains the right tool; this pathway is
        an accessible alternative when that equipment is not available.</p>

        <h3>✋ Gesture</h3>
        <p>Collect labelled example poses for your own custom gestures, then train a
        classifier — KNN or Random Forest, both are available, try either and keep
        whichever recognizes your gestures more reliably — to recognize them live
        from the webcam feed. Like Motion Capture, starting recognition
        auto-detects and drives whichever hand(s) the camera sees and auto-starts
        their simulations; a held gesture is sent to the real hand automatically
        once it's confirmed for a few frames.</p>

        <h3>⚙ Settings</h3>
        <p>Connection settings per hand, default power-on parameters, MuJoCo
        simulation tuning (speed, smoothing, force/tactile approach), and the
        dark/light theme toggle (Appearance section — takes effect the next time
        you start the app). Hand model XML files are always auto-detected next to
        the app itself; there's no path to configure.</p>

        <h3>📋 Log</h3>
        <p>Click ▶ Start Logging to begin recording every connected hand's angle,
        force, current, temperature, and error state to a CSV file. Tick
        <i>Include tactile arrays</i> before starting to also record all 1062
        tactile values of each hand in every row (larger files). Click again to
        stop; the tab then shows the saved file's contents here (if it isn't too
        large) so you can quickly check what was captured.</p>

        <h3>ℹ About</h3>
        <p>Project description, authorship, license, and hardware/compatibility
        details.</p>

        <h3>Tips</h3>
        <ul>
        <li>The app is fully usable without any physical hand connected — every
        tab's simulation and teleoperation features work standalone, which also
        makes the tool useful for learning or teaching dexterous-hand concepts.
        The Control tab's Simulation-only mode is the most direct way to do this
        with manual control; Motion Capture and Gesture work standalone too since
        they only need a webcam, no hand.</li>
        <li>If a feature is greyed out, an optional Python package it depends on is
        probably missing — see <code>requirements.txt</code> in the project folder.</li>
        <li>Switching between dark and light mode (Settings tab) takes effect the
        next time you start the app.</li>
        <li>Only one MuJoCo simulation per hand runs at a time across the whole
        app — this is intentional, for smooth frame rates, not a bug.</li>
        </ul>
        """)
        outer.addWidget(browser)


class AboutTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        content = QWidget()
        root=QVBoxLayout(content); root.setSpacing(10); root.setContentsMargins(12,12,12,12)

        # ── Header ───────────────────────────────────────────────────
        title=QLabel(f"{APP_NAME}  v{APP_VER}")
        title.setStyleSheet(f"font-size:{FS_L+2}pt;font-weight:bold;color:#58a6ff;")
        root.addWidget(title)
        tagline = QLabel("Open-source dual-hand control &amp; teleoperation software "
                          "for the Inspire Hand RH56 Series")
        tagline.setTextFormat(Qt.RichText)
        tagline.setWordWrap(True)
        tagline.setStyleSheet(f"color:{PAL['dim']};font-size:{FS}pt;font-style:italic;")
        root.addWidget(tagline)
        desc = QLabel(APP_DESCRIPTION)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color:{PAL['text']};font-size:{FS}pt;padding:6px 0;")
        root.addWidget(desc)

        # ── Authors & citation ────────────────────────────────────
        au_gb = QGroupBox("Authors & Citation"); au_l = QVBoxLayout(au_gb)
        any_author = False
        for a in APP_AUTHORS:
            if not a.get("name"): continue
            any_author = True
            line = a["name"]
            if a.get("affiliation"): line += f"  —  {a['affiliation']}"
            if a.get("orcid"):
                line += (f'   (ORCID: <a href="https://orcid.org/{a["orcid"]}" '
                          f'style="color:#58a6ff;">{a["orcid"]}</a>)')
            al = QLabel(line); al.setWordWrap(True)
            al.setTextFormat(Qt.RichText); al.setOpenExternalLinks(True)
            al.setStyleSheet(f"color:{PAL['text']};font-size:{FS}pt;")
            au_l.addWidget(al)
        if not any_author:
            au_l.addWidget(QLabel("(authors not yet configured — see APP_AUTHORS)"))
        lic = QLabel(f"License: {APP_LICENSE}")
        lic.setStyleSheet(f"color:{PAL['dim']};font-size:{FS_S}pt;")
        au_l.addWidget(lic)
        repo = QLabel(f'Source code: <a href="{APP_REPO_URL}" style="color:#58a6ff;">{APP_REPO_URL}</a>')
        repo.setTextFormat(Qt.RichText); repo.setOpenExternalLinks(True)
        repo.setStyleSheet(f"font-size:{FS_S}pt;")
        au_l.addWidget(repo)
        cite = QLabel(f"How to cite: {APP_CITE_NOTE}")
        cite.setWordWrap(True)
        cite.setStyleSheet(f"color:{PAL['dim']};font-size:{FS_S}pt;font-style:italic;")
        au_l.addWidget(cite)
        root.addWidget(au_gb)

        # ── Hardware & compatibility ─────────────────────────────────
        hw_gb = QGroupBox("Hardware & Compatibility"); hw_g = QGridLayout(hw_gb)
        hw_info = [
            ("Target hardware",  "Inspire Hand RH56 series specially the DFTP model — left & right, 6 actuated DOF each"),
            ("Device interface", "Modbus TCP over Ethernet (raw socket implementation, no vendor SDK required)"),
            ("Simulation",       "MuJoCo 3.x — STL meshes + MJCF kinematic/dynamic model, per-hand XML"),
            ("Input modalities", "Manual sliders/presets · webcam motion capture (MediaPipe) · trained gesture classifier"),
            ("Sensing",          "Per-finger + palm tactile force sensors, read back and visualised live"),
            ("Operating system", "Windows 10/11 (primary, tested); Linux compatible with minor path adjustments"),
        ]
        for i,(k,v) in enumerate(hw_info):
            kl=QLabel(k+":"); kl.setStyleSheet(f"color:{PAL['dim']};")
            vl=QLabel(v); vl.setWordWrap(True); vl.setStyleSheet(f"color:{PAL['text']};")
            hw_g.addWidget(kl,i,0); hw_g.addWidget(vl,i,1)
        root.addWidget(hw_gb)

        root.addWidget(QLabel(f"Run:  python \"{Path(sys.argv[0]).name}\""))
        root.addStretch()

        scroll.setWidget(content)
        outer.addWidget(scroll)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Dexterous Hand Controller  v{APP_VER}")
        self.resize(1620,980); self.setMinimumSize(1000,680)

        self.sessions=[
            HandSession("Left",  HAND_L_IP,"#4FC3F7",0.0),
            HandSession("Right", HAND_R_IP,"#EF9A9A",math.pi/4),
        ]
        
        # Initialize 6 filters PER HAND (so left and right don't mix)
        self.filters = {
            0: [OneEuroFilter(freq=30, mincutoff=0.5, beta=0.05) for _ in range(6)],
            1: [OneEuroFilter(freq=30, mincutoff=0.5, beta=0.05) for _ in range(6)]
        }

        self._shutting=False
        self._log_ctr=[0,0]
        self._last_state=[None,None]   # last polled (angle,force) per session — used to
        self._last_tac  =[None,None]   # last tactile frame per session, replayed on tab switch
                                        # detect "finger was force-stopped, now reopening"
        self._hw_seq=[HWSequencer(s, self) for s in self.sessions]
        self._executors:List[Optional[ActionExec]]=[None,None]

        self._build_ui()
        self._wire()

        # ── UI REFRESH TIMER — reads StateBuffers on the main thread ──────
        self._ui_timer=QTimer(self)
        self._ui_timer.timeout.connect(self._ui_tick)
        self._ui_timer.start(UI_HZ_MS)

        self._load_settings()   # restore everything from last session, if any

    # ── UI construction ───────────────────────────────────────────────────
    def _build_ui(self):
        tb=QToolBar("Main"); tb.setMovable(False); tb.setIconSize(QSize(20,20))
        self.addToolBar(Qt.TopToolBarArea,tb)
        for txt,fn in [("🔧 Calibrate All",self._calibrate_all),
                       ("🚫 Clear Errors",  self._clrerr_all),
                       ("💾 Save Flash",    self._save_all),
                       ("📷 Screenshot",    self._screenshot)]:
            b=_mk_btn(txt,PAL['btn_neutral']); b.clicked.connect(fn); b.setFixedHeight(34); tb.addWidget(b)

        cw=QWidget(); self.setCentralWidget(cw)
        vl=QVBoxLayout(cw); vl.setContentsMargins(5,0,5,5); vl.setSpacing(4)

        self.conn_panel=DualConnPanel(self.sessions); vl.addWidget(self.conn_panel)

        self.tabs=QTabWidget()
        self.dash_tab   =DashboardTab(self.sessions)
        self.tac_tab    =TactileTab(self.sessions)
        self.view3d_tab =View3DTab(self.sessions)
        self.ctrl_tab   =ControlTab(self.sessions)
        self.act_tab    =ActionTab(self.sessions)
        self.mocap_tab  =MotionCaptureTab(self.sessions)
        self.gest_tab   =GestureRecognitionTab(self.sessions)
        self.sett_tab   =SettingsTab(self.sessions)
        self.log_tab    =LogTab(self.sessions)
        self.about_tab  =AboutTab()
        self.help_tab   =HelpTab()

        self.tabs.addTab(self.dash_tab,   "📊 Dashboard")
        self.tabs.addTab(self.tac_tab,    "🖐 Tactile Map")
        self.tabs.addTab(self.view3d_tab, "🗿 3-D View")
        self.tabs.addTab(self.ctrl_tab,   "🎮 Control")
        self.tabs.addTab(self.act_tab,    "🎬 Actions")
        self.tabs.addTab(self.mocap_tab,  "📷 Motion Capture")
        self.tabs.addTab(self.gest_tab,   "🤌 Gesture")
        self.tabs.addTab(self.sett_tab,   "⚙ Settings")
        self.tabs.addTab(self.log_tab,    "📋 Log")
        self.tabs.addTab(self.about_tab,  "ℹ About")
        self.tabs.addTab(self.help_tab,   "❓ Help")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        vl.addWidget(self.tabs)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready — connect hands above or enable Demo mode")

    def _wire(self):
        self.conn_panel.connect_req.connect(self._on_connect)
        self.conn_panel.disconnect_req.connect(self._on_disconnect)
        self.ctrl_tab.send_ang.connect(self._send_ang)
        self.ctrl_tab.send_frc.connect(self._send_frc)
        self.ctrl_tab.disconnect_req.connect(self._on_disconnect)
        self.ctrl_tab.send_spd.connect(self._send_spd)
        self.dash_tab.send_ang.connect(self._send_ang)
        self.dash_tab.send_frc.connect(self._send_frc)
        self.dash_tab.send_spd.connect(self._send_spd)
        self.act_tab.execute_action.connect(self._run_action)
        self.mocap_tab.send_angles.connect(self._mocap_angles)
        self.gest_tab.send_angles.connect(self._gesture_angles)
        self.sett_tab.calib_sig.connect(lambda i:self._cmd(i,'calib'))
        self.sett_tab.clrerr_sig.connect(lambda i:self._cmd(i,'clrerr'))
        self.sett_tab.save_sig.connect(lambda i:self._cmd(i,'save'))
        self.sett_tab.reset_sig.connect(lambda i:self._cmd(i,'reset'))
        self.sett_tab.dspeed_sig.connect(lambda i,v:self.sessions[i].com.set_dspeed(v) if not self.sessions[i].demo_mode else None)
        self.sett_tab.dforce_sig.connect(lambda i,v:self.sessions[i].com.set_dforce(v) if not self.sessions[i].demo_mode else None)
        self._sync_ctrl_and_drawer()

    def _sync_ctrl_and_drawer(self):
        """The Control tab and the Dashboard's hidden drawer each have their
        OWN SingleHandControl instance (so both can show a live MuJoCo/view
        independently) — but that means force-limit / tactile-threshold
        spinboxes and stop checkboxes were two unlinked copies, drifting
        apart whenever you changed one but not the other. Mirror them live
        in both directions so they always agree."""
        def link_spin(a, b):
            def on_a(v):
                if b.value() != v: b.blockSignals(True); b.setValue(v); b.blockSignals(False)
            def on_b(v):
                if a.value() != v: a.blockSignals(True); a.setValue(v); a.blockSignals(False)
            a.valueChanged.connect(on_a); b.valueChanged.connect(on_b)
        def link_check(a, b):
            def on_a(v):
                if b.isChecked() != v: b.blockSignals(True); b.setChecked(v); b.blockSignals(False)
            def on_b(v):
                if a.isChecked() != v: a.blockSignals(True); a.setChecked(v); a.blockSignals(False)
            a.toggled.connect(on_a); b.toggled.connect(on_b)

        for i in range(len(self.sessions)):
            try:
                cpanel = self.ctrl_tab._panels[i]
                dpanel = self.dash_tab._drawer_controls[i]
                for d in range(6):
                    link_spin(cpanel._fsp[d], dpanel._fsp[d])
                    link_spin(cpanel._tsp[d], dpanel._tsp[d])
                    link_check(cpanel._rows[d].force_cb,      dpanel._rows[d].force_cb)
                    link_check(cpanel._rows[d].stop_force_cb, dpanel._rows[d].stop_force_cb)
                    link_check(cpanel._rows[d].stop_tac_cb,   dpanel._rows[d].stop_tac_cb)
                # Mirror the FULL slider state via send_ang (fires with the
                # complete, already-correct _ang list after every drag,
                # including fingers moved by group-sync) instead of trying
                # to listen to individual slider signals.
                cpanel.send_ang.connect(dpanel.sync_from)
                dpanel.send_ang.connect(cpanel.sync_from)
            except Exception as e:
                print(f"[sync] ctrl/drawer link failed for hand {i}: {e}")

    # ── UI tick (runs on main thread only) ────────────────────────────────
    def _ui_tick(self):
        """Fan telemetry out to the widgets.

        Only the visible tab is refreshed. Plot curves and tactile heat maps
        are comparatively expensive to repaint, and updating hidden copies of
        them costs the same as visible ones while showing nothing -- with a
        50 Hz feed on two hands that was enough to halve the simulation frame
        rate. The newest sample per hand is kept and replayed by
        _on_tab_changed(), so a tab is current the moment it is shown.
        """
        if self._shutting: return
        cur = self.tabs.currentWidget()
        for idx, sess in enumerate(self.sessions):
            s, td = sess.buf.take()
            if sess.poller is None:
                # Feed stopped. Frames already queued by the poller must be
                # discarded rather than applied, otherwise a late sample
                # re-arms the tactile overlay that _on_disconnect just
                # cleared and the sims keep paying to draw it.
                continue
            if s is not None:
                self._last_state[idx] = s
                try:
                    if cur is self.dash_tab:   self.dash_tab.on_state(idx, s)
                    if cur is self.ctrl_tab:   self.ctrl_tab.on_state(idx, s)
                    if cur is self.view3d_tab: self.view3d_tab.on_state(idx, s)
                    self._log_ctr[idx] += 1
                    if self._log_ctr[idx] >= 8:
                        # td comes from the same poller cycle as s; fall back
                        # to the last frame when this tick carried none.
                        self._log_ctr[idx] = 0
                        self.log_tab.feed(sess.name, s, td if td is not None else self._last_tac[idx])
                except Exception as e:
                    print(f"[UI state {idx}] {e}")
            if td is not None:
                self._last_tac[idx] = td
                try:
                    if cur is self.dash_tab:   self.dash_tab.on_tac(idx, td)
                    if cur is self.tac_tab:    self.tac_tab.on_tac(idx, td)
                    if cur is self.view3d_tab: self.view3d_tab.on_tac(idx, td)
                    if cur is self.ctrl_tab:   self.ctrl_tab.on_tac(idx, td)
                    if cur is self.mocap_tab and idx < len(self.mocap_tab._mocap_mj):
                        self.mocap_tab._mocap_mj[idx].set_tactile(td)
                    if cur is self.gest_tab and idx < len(self.gest_tab._gest_mj):
                        self.gest_tab._gest_mj[idx].set_tactile(td)
                except Exception as e:
                    print(f"[UI tac {idx}] {e}")

    def _clear_sim_tactile(self, idx: int):
        """Blank the tactile overlay on every MuJoCo view of hand `idx`."""
        self._last_tac[idx] = None
        groups = []
        for tab, attr in ((self.view3d_tab, "_mj_widgets"),
                          (self.mocap_tab,  "_mocap_mj"),
                          (self.gest_tab,   "_gest_mj")):
            groups.append(getattr(tab, attr, None))
        for g in groups:
            if g and idx < len(g):
                try: g[idx].set_tactile({})
                except Exception: pass
        try:
            panel = self.ctrl_tab._panels[idx]
            if panel._mj_ctrl is not None: panel._mj_ctrl.set_tactile({})
        except Exception: pass
        try:
            for w in self.dash_tab.findChildren(MuJoCoWidget):
                w.set_tactile({})
        except Exception: pass

    def _on_tab_changed(self, _index=None):
        """Replay the latest telemetry into the tab being shown, so it is not
        stale after having been skipped while hidden."""
        if self._shutting: return
        cur = self.tabs.currentWidget()
        for idx in range(len(self.sessions)):
            s  = self._last_state[idx] if idx < len(self._last_state) else None
            td = self._last_tac[idx]   if idx < len(self._last_tac)   else None
            try:
                if s is not None:
                    if cur is self.dash_tab:   self.dash_tab.on_state(idx, s)
                    if cur is self.ctrl_tab:   self.ctrl_tab.on_state(idx, s)
                    if cur is self.view3d_tab: self.view3d_tab.on_state(idx, s)
                if td is not None:
                    if cur is self.dash_tab:   self.dash_tab.on_tac(idx, td)
                    if cur is self.tac_tab:    self.tac_tab.on_tac(idx, td)
                    if cur is self.view3d_tab: self.view3d_tab.on_tac(idx, td)
                    if cur is self.ctrl_tab:   self.ctrl_tab.on_tac(idx, td)
                    if cur is self.mocap_tab and idx < len(self.mocap_tab._mocap_mj):
                        self.mocap_tab._mocap_mj[idx].set_tactile(td)
                    if cur is self.gest_tab and idx < len(self.gest_tab._gest_mj):
                        self.gest_tab._gest_mj[idx].set_tactile(td)
            except Exception as e:
                print(f"[UI tabswitch {idx}] {e}")

    # ── connect / disconnect ──────────────────────────────────────────────
    def _on_connect(self,idx,ip,port,dev,demo):
        sess=self.sessions[idx]; sess.demo_mode=demo; sess.ip=ip
        # Restart the traces so the x axis reads seconds since THIS connection.
        try:
            for pl in self.dash_tab.findChildren(PlotPanel): pl.reset()
        except Exception: pass
        # Force a fresh slider/setting sync to this connection's actual pose
        try:
            self.ctrl_tab._panels[idx]._hw_synced = False
            self.ctrl_tab._panels[idx].reset_live_readings()
            # A live connection or demo feed is now driving this hand -- turn
            # off Simulation-only mode if it was on, so the two can never
            # fight over the same MuJoCo widget (mutual exclusion, both ways:
            # checking Simulation-only already disconnects, see disconnect_req).
            simcb = getattr(self.ctrl_tab._panels[idx], '_sim_only_cb', None)
            if simcb is not None and simcb.isChecked():
                simcb.setChecked(False)
        except Exception: pass
        try: self.dash_tab._drawer_controls[idx]._hw_synced = False
        except Exception: pass
        if not demo:
            self.statusBar().showMessage(f"Connecting {sess.name} → {ip}:{port} …")
            QApplication.processEvents()
            if not sess.com.connect(ip,port,dev):
                QMessageBox.warning(self,"Connection failed",
                    f"{sess.name}: cannot reach {ip}:{port}\nFalling back to Demo mode.")
                sess.demo_mode=True
                # tick demo checkbox
                self.conn_panel._fields[idx][3].setChecked(True)

        poller=DataPoller(sess)
        # lost_conn: must use queued so it fires on main thread
        poller.lost_conn.connect(lambda i=idx:self._on_disconnect(i),Qt.QueuedConnection)
        poller.set_tac(self.sett_tab.poll_tac.isChecked())
        poller.set_interval(self.sett_tab.poll_ms.value())
        poller.start(); sess.poller=poller
        tag="Demo" if sess.demo_mode else f"{ip}:{port} id={dev}"
        self.conn_panel.set_state(idx,True,tag)
        self.statusBar().showMessage(f"{sess.name} hand connected [{tag}]")

    def _on_disconnect(self,idx):
        if self._shutting: return
        sess=self.sessions[idx]
        if sess.poller and sess.poller.isRunning():
            sess.poller.stop(); sess.poller.quit(); sess.poller.wait(3000)
        sess.poller=None; sess.com.disconnect()
        sess.demo_mode = False
        try: self.ctrl_tab._panels[idx].reset_live_readings()
        except Exception: pass
        # No feed left for this hand: clear the tactile frame held by every
        # simulation view of it. Renderers are cached and a running sim keeps
        # redrawing whatever it last received, so without this a stopped demo
        # goes on costing a few hundred sphere draws per frame indefinitely.
        try:
            for _ in range(4): sess.buf.take()   # drop frames queued before stop
        except Exception: pass
        self._clear_sim_tactile(idx)
        self.conn_panel.set_state(idx,False)
        self.statusBar().showMessage(f"{sess.name} hand disconnected")

    # ── commands ──────────────────────────────────────────────────────────
    def _update_mocap_sims(self, idx: int, angles: List[int]):
        fake_s = {'angle': angles, 'force': [0]*6, 'current': [0]*6,
                  'temp': [0]*6, 'error': [0]*6, 'status': [0]*6, 'pos': [0]*6}

    def _mocap_angles(self, idx: int, angles: List[int]):
        """Motion Capture: continuous live tracking using One-Euro filter + Deadband."""
        current_cutoff = SimTuning.smooth_alpha
        
        # We need a place to store the 'last sent' angles to calculate the deadband.
        # If it doesn't exist yet, create it.
        if not hasattr(self, '_last_mocap_sent'):
            self._last_mocap_sent = {0: [1000]*6, 1: [1000]*6}
            
        filtered_angles = []
        for dof in range(6):
            f = self.filters[idx][dof]
            f.mincutoff = current_cutoff

            f.beta = 0.002  
            
            clean_val = int(f(angles[dof]))
            
            # THE DEADBAND: 
            # Only update the target if the camera moved the finger by MORE than 8 units.
            # This completely kills resting micro-jitter.
            if abs(clean_val - self._last_mocap_sent[idx][dof]) > 8:
                filtered_angles.append(clean_val)
            else:
                filtered_angles.append(self._last_mocap_sent[idx][dof])

        self._last_mocap_sent[idx] = list(filtered_angles)

        self._send_ang(idx, filtered_angles)
        self._update_mocap_sims(idx, filtered_angles)

    def _gesture_angles(self, idx: int, angles: List[int]):
        """Gesture tab ONLY: a discrete, sudden jump to a whole new pose —
        this is the one case where the thumb can visually/physically punch
        through the index mid-transition, so (and ONLY so) this path goes
        through the hold-thumb/hold-finger sequencer. Controlled by
        SimTuning.hwseq_enabled (disabled by default, no UI toggle)."""
        sess = self.sessions[idx]
        if sess.demo_mode or not SimTuning.hwseq_enabled:
            self._send_ang(idx, angles)
        else:
            self._hw_seq[idx].send(angles)
        self._update_mocap_sims(idx, angles)

    def _send_ang(self,idx,v):
        sess=self.sessions[idx]
        if sess.demo_mode: return
        
        last = self._last_state[idx]
        if last is not None:
            try:
                last_angle = last.get('angle',[0]*6)
                last_force = last.get('force',[0]*6)
                limits = [s.value() for s in self.ctrl_tab._panels[idx]._fsp]
                if any(v[i] > last_angle[i]+10 and abs(last_force[i]) >= limits[i]*0.9
                       for i in range(6)):
                    sess.com.clrerr()
            except Exception:
                pass
                
        # Send the target to the smooth sequencer instead of blasting it directly to the hand
        self._hw_seq[idx].send(v)
    def _send_frc(self,idx,v):
        if not self.sessions[idx].demo_mode: self.sessions[idx].com.set_forces(v)
    def _send_spd(self,idx,v):
        if not self.sessions[idx].demo_mode: self.sessions[idx].com.set_speeds(v)
    def _cmd(self,idx,cmd):
        if not self.sessions[idx].demo_mode: getattr(self.sessions[idx].com,cmd)()
    def _calibrate_all(self):
        QMessageBox.information(self,"Calibrate","Open all fingers (not touching anything).\nCalibrating…")
        for i,sess in enumerate(self.sessions):
            if not sess.demo_mode: self._cmd(i,'calib')
    def _clrerr_all(self):
        for i in range(len(self.sessions)): self._cmd(i,'clrerr')
        self.statusBar().showMessage("Errors cleared")
    def _save_all(self):
        for i in range(len(self.sessions)): self._cmd(i,'save')
        self.statusBar().showMessage("Flash saved on all hands")
    def _run_action(self,idx,steps):
        if self._executors[idx] and self._executors[idx].isRunning(): return
        ex=ActionExec(steps)
        ex.do_ang.connect(lambda v,i=idx:self._send_ang(i,v))
        ex.do_frc.connect(lambda v,i=idx:self._send_frc(i,v))
        ex.do_spd.connect(lambda v,i=idx:self._send_spd(i,v))
        ex.status.connect(self.statusBar().showMessage)
        ex.start(); self._executors[idx]=ex
    def _screenshot(self):
        # Save next to the log file so captures and recorded data from the
        # same session stay together.
        try:
            base = Path(self.log_tab.path.text()).expanduser().parent
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            base = Path.home()
        p = base / f"inspire_{datetime.now():%Y%m%d_%H%M%S}.png"
        self.grab().save(str(p))
        self.statusBar().showMessage(f"Screenshot saved: {p}")

    def _settings_widgets(self):
        """List of (unique_key, widget) for everything restored on next launch."""
        items = []
        for i, sess in enumerate(self.sessions):
            ip, port, dev, demo, _ = self.conn_panel._fields[i]
            items += [
                (f"conn/{sess.name}/ip",   ip),
                (f"conn/{sess.name}/port", port),
                (f"conn/{sess.name}/dev",  dev),
                (f"conn/{sess.name}/demo", demo),
            ]
        items += [
            ("settings/poll_ms",  self.sett_tab.poll_ms),
            ("settings/poll_tac", self.sett_tab.poll_tac),
            ("settings/speed",    self.sett_tab.speed_sl),
            ("settings/smooth",   self.sett_tab.smooth_sl),
            ("settings/fsmooth",  self.sett_tab.fsmooth_sl),
        ]
        for i, sess in enumerate(self.sessions):
            panel = self.ctrl_tab._panels[i]
            for d in range(6):
                items += [
                    (f"ctrl/{sess.name}/force_limit/{d}",  panel._fsp[d]),
                    (f"ctrl/{sess.name}/tac_thresh/{d}",   panel._tsp[d]),
                    (f"ctrl/{sess.name}/sync_cb/{d}",       panel._rows[d].force_cb),
                    (f"ctrl/{sess.name}/stop_force_cb/{d}", panel._rows[d].stop_force_cb),
                    (f"ctrl/{sess.name}/stop_tac_cb/{d}",   panel._rows[d].stop_tac_cb),
                ]
        items += [
            ("gesture/hold",     self.gest_tab.hold_sb),
            ("gesture/cam",      self.gest_tab.cam_cb),
        ]
        items += [
            ("mocap/mirror",   self.mocap_tab.mirror_cb),
            ("mocap/send",     self.mocap_tab.send_cb),
            ("mocap/smooth",   self.mocap_tab.smooth_sb),
            ("mocap/cam",      self.mocap_tab.cam_cb),
        ]
        items += [("log/tactile", self.log_tab.tac_cb)]
        return items

    def _save_settings(self):
        try:
            qs = QSettings("InspireHandController", "App")
            qs.setValue("geometry", self.saveGeometry())
            for key, w in self._settings_widgets():
                try:
                    if isinstance(w, QCheckBox):
                        qs.setValue(key, w.isChecked())
                    elif isinstance(w, (QSpinBox, QSlider)):
                        qs.setValue(key, w.value())
                    elif isinstance(w, QComboBox):
                        qs.setValue(key, w.currentIndex())
                    elif isinstance(w, QLineEdit):
                        qs.setValue(key, w.text())
                except Exception:
                    pass
            qs.sync()
        except Exception as e:
            print(f"[settings] save failed: {e}")

    def _load_settings(self):
        try:
            qs = QSettings("InspireHandController", "App")
            geo = qs.value("geometry")
            if geo is not None:
                try: self.restoreGeometry(geo)
                except Exception: pass
            for key, w in self._settings_widgets():
                if not qs.contains(key): continue
                try:
                    if isinstance(w, QCheckBox):
                        val = qs.value(key)
                        w.setChecked(val in (True, "true", "1", 1))
                    elif isinstance(w, (QSpinBox, QSlider)):
                        w.setValue(int(qs.value(key)))
                    elif isinstance(w, QComboBox):
                        idx = int(qs.value(key))
                        if 0 <= idx < w.count(): w.setCurrentIndex(idx)
                    elif isinstance(w, QLineEdit):
                        w.setText(str(qs.value(key)))
                except Exception:
                    pass
        except Exception as e:
            print(f"[settings] load failed: {e}")

    def closeEvent(self,ev):
        self._save_settings()
        self._shutting=True; self._ui_timer.stop()
        # Stop motion capture workers first
        try: self.mocap_tab.cleanup()
        except: pass
        try: self.gest_tab.cleanup()
        except: pass
        for ex in self._executors:
            if ex and ex.isRunning(): ex.quit(); ex.wait(2000)
        for sess in self.sessions:
            if sess.poller and sess.poller.isRunning():
                sess.poller.stop(); sess.poller.quit(); sess.poller.wait(3000)
            sess.com.disconnect()
        ev.accept()

# ──────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────
def main():
    if _IS_WIN:
        os.environ.setdefault("QT_FONT_DPI","96")
        os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR","1")

    _app.setApplicationName("Inspire Hand Controller")
    _app.setApplicationVersion(APP_VER)
    # Every frame allocates short-lived numpy arrays and Qt objects, which
    # fills CPython's youngest GC generation quickly. The default thresholds
    # then trigger a cyclic collection several times a second, and an
    # occasional sweep is long enough (~100 ms) to show as a visible stall in
    # the traces and the tactile map. Nothing here relies on prompt cyclic
    # collection -- reference counting still frees objects immediately -- so
    # the generational thresholds are raised to make sweeps rarer.
    import gc as _gc
    _gc.set_threshold(20000, 50, 50)

    _app.setFont(QFont(AFNT, FS))
    _app.setStyleSheet(APP_QSS)

    win=MainWindow()
    win.showMaximized()   # always open fullscreen/maximised

    if _IS_WIN and not LIGHT_MODE:
        try:
            import ctypes
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            hwnd = int(win.winId())
            val  = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    missing=[]
    if not MB_OK: missing.append("pymodbus")
    if not GL_OK: missing.append("PyOpenGL PyOpenGL_accelerate")
    if not TM_OK: missing.append("trimesh")
    if not PG_OK: missing.append("pyqtgraph")
    if missing:
        win.statusBar().showMessage(
            "⚠ Missing:  pip install " + " ".join(missing) + "   (Demo mode active)")

    sys.exit(_app.exec_())


if __name__ == "__main__":
    # Worker processes must start from a fresh interpreter. Linux defaults to
    # fork, which copies the running Qt/OpenGL state into the camera worker;
    # the copied heap is corrupt by the second camera start and the worker
    # aborts. Spawn is already the default on Windows and macOS, so this only
    # changes Linux behaviour.
    import multiprocessing
    multiprocessing.set_start_method("spawn", force=True)
    main()
