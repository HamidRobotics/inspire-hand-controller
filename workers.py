"""
workers.py — Multiprocessing workers for Inspire Hand Controller
================================================================
Isolated processes (no GIL conflicts):

  CameraWorkerProcess   — OpenCV + MediaPipe / ONNX landmark inference
  MuJoCoWorkerProcess   — MuJoCo 1000 Hz sim + 60 Hz offscreen render
  ModbusWorkerProcess   — Modbus TCP 100 Hz polling

All communication via multiprocessing.Queue / multiprocessing.Array (shared mem).
Import in main Qt process; start/stop via .start() / .stop().
"""

import multiprocessing as mp
import multiprocessing.sharedctypes as sct
import ctypes, time, math, json, os, struct, threading
import numpy as np
from pathlib import Path
from typing import Optional, List, Dict
try:
    from calibration import BUILTIN_GESTURES
except Exception:
    BUILTIN_GESTURES = {}   # fallback if calibration.py isn't on the path for some reason
try:
    from calibration import load_model, predict_with_model
except Exception:
    load_model = predict_with_model = None

# ─────────────────────────────────────────────────────────────────────────────
#  Shared constants
# ─────────────────────────────────────────────────────────────────────────────
CAM_W, CAM_H   = 640, 480        # camera capture resolution
RENDER_W, RENDER_H = 1280, 960    # mujoco render resolution (matches <global offwidth/offheight> in the hand XMLs -- keep these in sync if you change either)
N_DOF          = 6

# ─────────────────────────────────────────────────────────────────────────────
#  CAMERA ENUMERATION  — real device names, cross-platform
# ─────────────────────────────────────────────────────────────────────────────
_CAM_CACHE: Optional[list] = None

def enumerate_cameras(force_refresh: bool = False) -> list:
    #Return list of (index, display_name) for every available camera.
    global _CAM_CACHE
    if _CAM_CACHE is not None and not force_refresh:
        return list(_CAM_CACHE)
    import sys, os
    results = []

    # Suppress verbose camera driver output on Windows (VCAMDS/NVIDIA spam)
    if sys.platform == 'win32':
        import ctypes
        # Redirect C-level stderr to NUL during camera probing
        try:
            import ctypes.util
            libc = ctypes.CDLL(ctypes.util.find_library('c') or 'msvcrt')
            devnul = ctypes.c_void_p(libc.fopen(b'NUL', b'w'))
            stderr_fd = 2
            old_fd = libc._dup(stderr_fd)
            libc._dup2(libc._fileno(devnul), stderr_fd)
        except Exception:
            old_fd = None; devnul = None; libc = None
    else:
        old_fd = None; devnul = None; libc = None

    try:
        import cv2
        if sys.platform == 'win32':
            # Get real names via PowerShell
            names_by_index = {}
            try:
                import subprocess, json
                ps = ("Get-PnpDevice -Class Camera -Status OK | "
                      "Select-Object -ExpandProperty FriendlyName | ConvertTo-Json")
                out = subprocess.run(['powershell','-Command',ps],
                    capture_output=True, text=True, timeout=4)
                raw = out.stdout.strip()
                if raw:
                    names = json.loads(raw) if raw.startswith('[') else [json.loads(raw)]
                    for i, n in enumerate(names):
                        names_by_index[i] = str(n)
            except Exception:
                pass

            for i in range(6):
                cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
                if cap.isOpened():
                    name = names_by_index.get(i)
                    if not name:
                        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        name = f"Camera {i}  ({w}×{h})"
                    results.append((i, name))
                cap.release()

        elif sys.platform.startswith('linux'):
            for i in range(8):
                if not os.path.exists(f'/dev/video{i}'): continue
                cap = cv2.VideoCapture(i)
                if not cap.isOpened(): cap.release(); continue
                cap.release()
                try:
                    name = open(f'/sys/class/video4linux/video{i}/name').read().strip()
                    results.append((i, f"{name}  (/dev/video{i})"))
                except Exception:
                    results.append((i, f"Camera {i}  (/dev/video{i})"))
        else:
            for i in range(4):
                cap = cv2.VideoCapture(i)
                if cap.isOpened(): results.append((i, f"Camera {i}")); cap.release()
    finally:
        # Restore stderr on Windows
        if sys.platform == 'win32' and old_fd is not None and libc is not None:
            try:
                libc._dup2(old_fd, 2)
                libc._close(old_fd)
                if devnul: libc.fclose(devnul)
            except Exception:
                pass

    out = results if results else [(i, f"Camera {i}") for i in range(4)]
    _CAM_CACHE = list(out)
    return out

def lm_to_dof(pts: np.ndarray,
              range_min: np.ndarray,
              range_max: np.ndarray) -> List[int]:
    """
    pts: (21,3) normalised landmarks.
    range_min/max: (6,) calibrated open/close reference vectors.
    Returns list of 6 ints in [0,1000].
    """
    def _ang(v1, v2):
        c = float(np.dot(v1, v2) / (np.linalg.norm(v1)*np.linalg.norm(v2)+1e-9))
        return math.acos(max(-1., min(1., c)))

    def _curl(base, mcp, pip, tip):
        vp = pts[pip]-pts[mcp]; vd = pts[tip]-pts[pip]
        vb = pts[mcp]-pts[base]
        pip_a = _ang(vp/(np.linalg.norm(vp)+1e-9), vd/(np.linalg.norm(vd)+1e-9))
        mcp_a = _ang(vb/(np.linalg.norm(vb)+1e-9), vp/(np.linalg.norm(vp)+1e-9))
        return min(1.0, (pip_a + mcp_a) / (math.pi * 0.55))

    FINGER_PTS = [(0,17,18,20),(0,13,14,16),(0,9,10,12),(0,5,6,8)]
    raw = np.zeros(6)
    for i,(b,m,p,t) in enumerate(FINGER_PTS):
        raw[i] = _curl(b,m,p,t)
    # ── Thumb ─────────────────────────────────────────────────────────────
    # Palm normal (reused for abduction reference)
    pv1=pts[5]-pts[0]; pv2=pts[17]-pts[0]
    pn=np.cross(pv1,pv2); pn/=(np.linalg.norm(pn)+1e-9)

    # DOF 4 — thumb curl: angle at MCP (CMC→MCP→IP) + angle at IP (MCP→IP→TIP)
    # Same joint-angle approach as the four fingers — fixes the old broad-vector estimate
    v_cmc_mcp=pts[2]-pts[1]; v_mcp_ip=pts[3]-pts[2]; v_ip_tip=pts[4]-pts[3]
    raw[4]=min(1.0, (_ang(v_cmc_mcp, v_mcp_ip) + _ang(v_mcp_ip, v_ip_tip)) / math.pi)

    # DOF 5 — thumb abduction: how far the thumb rises out of the palm plane
    # Primary: out-of-plane elevation (dot with palm normal)
    thumb_long=pts[4]-pts[1]; thumb_long/=(np.linalg.norm(thumb_long)+1e-9)
    elev=abs(float(np.dot(thumb_long, pn)))          # 0=in palm, 1=perpendicular
    # Secondary: in-plane lateral spread from the index direction
    idx_dir=pv1/(np.linalg.norm(pv1)+1e-9)
    in_plane=pts[4]-pts[1]-float(np.dot(pts[4]-pts[1],pn))*pn
    in_plane/=(np.linalg.norm(in_plane)+1e-9)
    spread=max(0., 1.-float(np.dot(in_plane, idx_dir)))  # 0=closed, 1=fully open
    raw[5]=min(1.0, max(elev*1.5, spread))
    raw = np.clip(raw, 0., 1.)

    # Apply calibration range mapping
    rng = range_max - range_min
    rng = np.where(rng < 0.05, 1.0, rng)   # avoid div/0
    mapped = (raw - range_min) / rng
    mapped = np.clip(mapped, 0., 1.)
    # Hardware convention: 1000=open, 0=closed  (flip from raw curl which is 0=open)
    return [int((1-v)*1000) for v in mapped]


def _finger_spreads(pts: np.ndarray) -> List[float]:
    """
    Lateral spread between adjacent fingers via palm-plane projection.
    Returns [Pinky-Ring, Ring-Middle, Middle-Index] in [0,1].
    Uses MediaPipe 3-D z-depth so even subtle spread is detected.
    """
    mcps = np.array([pts[17], pts[13], pts[9],  pts[5]])
    tips = np.array([pts[20], pts[16], pts[12], pts[8]])
    pv1  = pts[5]  - pts[0]; pv2 = pts[17] - pts[0]
    pn   = np.cross(pv1, pv2); nrm = np.linalg.norm(pn)
    if nrm < 1e-9: return [0.0, 0.0, 0.0]
    pn /= nrm
    spreads = []
    for i in range(3):
        v1 = tips[i]-mcps[i]; v2 = tips[i+1]-mcps[i+1]
        v1p = v1 - np.dot(v1,pn)*pn; v2p = v2 - np.dot(v2,pn)*pn
        n1 = np.linalg.norm(v1p); n2 = np.linalg.norm(v2p)
        if n1 < 1e-9 or n2 < 1e-9: spreads.append(0.0); continue
        deg = math.degrees(math.acos(max(-1., min(1., float(np.dot(v1p/n1, v2p/n2))))))
        spreads.append(min(1.0, deg/30.0))
    return spreads


_GESTURE_MODEL = None
_GESTURE_MODEL_LOADED = False

def _get_gesture_model():
    """Load the user-trained model once per process (camera worker runs in
    its own subprocess) and reuse it on every frame."""
    global _GESTURE_MODEL, _GESTURE_MODEL_LOADED
    if not _GESTURE_MODEL_LOADED:
        _GESTURE_MODEL_LOADED = True
        try:
            _GESTURE_MODEL = load_model() if load_model else None
        except Exception:
            _GESTURE_MODEL = None
    return _GESTURE_MODEL


def classify_gesture(dofs: List[int],
                     personal: Optional[Dict] = None,
                     pts: Optional[np.ndarray] = None) -> tuple:
    """Return (name, confidence%).
    Tries the user-trained AI model first (see CalibrationDialog's "AI
    Gesture Training" — trained on YOUR OWN recorded samples). Falls back to
    weighted-distance template matching if no model has been trained yet,
    or if the model isn't confident about this particular frame.
    Uses all 6 DOFs (thumb-rotation weighted down slightly — it's the
    noisiest estimate) and bases confidence on BOTH how close the best
    match is AND how clearly it beats the runner-up template. A pose that's
    almost equidistant between two gestures now reports low confidence
    instead of confidently snapping to whichever is marginally closer —
    this is the standard fix for ambiguous near-miss misclassification.
    pts: (21,3) landmarks — enables Spread & Vulcan via lateral spread angles.
    """
    model = _get_gesture_model()
    if model is not None:
        try:
            label, mconf = predict_with_model(model, dofs)
            if mconf >= 55:
                return label, mconf
        except Exception:
            pass

    TEMPLATES = dict(BUILTIN_GESTURES)
    if personal:
        TEMPLATES.update(personal)

    # Per-DOF weights: thumb-rotation (idx 5) is the noisiest tracked value,
    # so it influences the match less than the four finger curls + thumb bend.
    W = [1.0, 1.0, 1.0, 1.0, 0.9, 0.6]

    dists = []
    for name, tmpl in TEMPLATES.items():
        d = math.sqrt(sum(w*(a-b)**2 for w,a,b in zip(W, dofs[:6], tmpl[:6])))
        dists.append((d, name))
    dists.sort(key=lambda x: x[0])
    best_d, best = dists[0]
    second_d = dists[1][0] if len(dists) > 1 else best_d + 1e9

    max_d     = math.sqrt(sum(w*1000.0**2 for w in W))
    closeness = max(0.0, 1 - best_d/max_d)                    # 0..1 — how near the best template
    margin    = max(0.0, min(1.0, (second_d-best_d)/250.0))   # 0..1 — how clearly it beats the runner-up
    conf      = int(100 * closeness * (0.5 + 0.5*margin))

    # Override with spread-based classification when landmarks available
    if pts is not None:
        sp = _finger_spreads(pts)   # [Pinky-Ring, Ring-Middle, Middle-Index]
        all_spread = all(s > 0.45 for s in sp)
        vulcan_gap = sp[1] > 0.55 and sp[1] > sp[0]*1.6 and sp[1] > sp[2]*1.6
        if all_spread:
            best = "🖐 Spread"; conf = int(min(100, min(sp)*180))
        elif vulcan_gap and dofs[2] < 300 and dofs[3] < 300:  # mid+index open
            best = "🖖 Vulcan"; conf = int(min(100, sp[1]*160))

    return (best, conf) if conf > 42 else ("Unknown", conf)


class GestureVoter:
    """Majority-vote over a short rolling window of recent per-frame
    classifications, to suppress single-frame flicker between adjacent
    gesture templates (the #1 cause of 'jittery'-feeling gesture detection).
    One instance per tracked hand; call update() every frame."""
    def __init__(self, window: int = 6):
        self.window = window
        self.buf: List[tuple] = []   # [(name, conf), ...]

    def update(self, name: str, conf: int) -> tuple:
        self.buf.append((name, conf))
        if len(self.buf) > self.window:
            self.buf.pop(0)
        names = [n for n, _ in self.buf]
        winner = max(set(names), key=names.count)
        votes  = names.count(winner)
        if winner == 'Unknown' or votes < (self.window // 2 + 1):
            return 'Unknown', 0   # no clear majority yet — stay Unknown rather than guess
        confs = [c for n, c in self.buf if n == winner]
        return winner, int(sum(confs) / len(confs))


def _camera_process(cfg: dict,
                    out_q:   mp.Queue,
                    frame_q: mp.Queue,
                    stop_ev: mp.Event):
    import cv2

    mode          = cfg.get('mode', 'mediapipe')
    cam_idx       = cfg.get('cam_idx', 0)
    mirror        = cfg.get('mirror', True)
    smooth_k      = max(1, cfg.get('smooth_k', 4))
    range_min     = np.array(cfg.get('range_min', [0.0]*6))
    range_max     = np.array(cfg.get('range_max', [1.0]*6))
    personal      = cfg.get('personal_gestures', None)
    detect_gest   = cfg.get('detect_gestures', False)

    cam_w         = int(cfg.get('cam_w', CAM_W))
    cam_h         = int(cfg.get('cam_h', CAM_H))


    if os.name == 'nt':
        cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW)
        if not cap.isOpened():          # fall back to auto-detect if DSHOW fails
            cap.release()
            cap = cv2.VideoCapture(cam_idx)
    else:
        cap = cv2.VideoCapture(cam_idx)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  cam_w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cam_h)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # don't accumulate stale frames -> lower latency

    # ── MediaPipe mode ─────────────────────────────────────────────────────
    if mode == 'mediapipe':
        import mediapipe as _mp
        _mp_h  = _mp.solutions.hands
        # MediaPipe's draw_landmarks() hardcodes a white background circle
        # behind every landmark dot INSIDE the library function itself —
        # changing the DrawingSpec color never touches that, it only colors
        # the dot drawn on TOP of the white one. So: skip draw_landmarks
        # entirely and draw the skeleton ourselves with plain cv2 calls —
        # zero white, full control. Colored per finger (matching the rest
        # of the app's finger color scheme) instead of one flat color.
        def _hex_bgr(h):
            h = h.lstrip('#'); r,g,b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
            return (b,g,r)
        _FINGER_COLOR = {
            'thumb':  _hex_bgr('#87CEEB'),
            'index':  _hex_bgr('#98FB98'),
            'middle': _hex_bgr('#FFD700'),
            'ring':   _hex_bgr('#FFB347'),
            'pinky':  _hex_bgr('#FF6B6B'),
            'wrist':  (180,180,180),
        }
        def _lm_group(i):
            if i == 0: return 'wrist'
            if 1  <= i <= 4:  return 'thumb'
            if 5  <= i <= 8:  return 'index'
            if 9  <= i <= 12: return 'middle'
            if 13 <= i <= 16: return 'ring'
            return 'pinky'
        def _draw_skeleton(frame, lm):
            h, w = frame.shape[:2]
            pts_px = [(int(l.x*w), int(l.y*h)) for l in lm.landmark]
            for i, j in _mp_h.HAND_CONNECTIONS:
                color = _FINGER_COLOR[_lm_group(max(i, j))]
                cv2.line(frame, pts_px[i], pts_px[j], color, 2)
            for i, p in enumerate(pts_px):
                cv2.circle(frame, p, 3, _FINGER_COLOR[_lm_group(i)], -1)
        hands_detector = _mp_h.Hands(
            static_image_mode=False,
            max_num_hands=cfg.get('max_hands', 2),
            min_detection_confidence=0.6,
            min_tracking_confidence=0.55)

        smooth_buf: List[List[List[float]]] = [[[] for _ in range(6)] for _ in range(2)]
        gest_voters = [GestureVoter(), GestureVoter()]   # one per hand (Left/Right)

        while not stop_ev.is_set():
            ok, frame = cap.read()
            if not ok: time.sleep(0.01); continue
            if mirror: frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            res = hands_detector.process(rgb)

            detected = []
            if res.multi_hand_landmarks and res.multi_handedness:
                for lm, hd in zip(res.multi_hand_landmarks, res.multi_handedness):
                    side = hd.classification[0].label   # 'Left' or 'Right'
                    hi   = 0 if side == 'Left' else 1
                    pts  = np.array([[l.x,l.y,l.z] for l in lm.landmark])
                    dofs = lm_to_dof(pts, range_min, range_max)

                    # Smooth
                    for di in range(6):
                        smooth_buf[hi][di].append(dofs[di])
                        if len(smooth_buf[hi][di]) > smooth_k:
                            smooth_buf[hi][di].pop(0)
                        dofs[di] = int(sum(smooth_buf[hi][di])/len(smooth_buf[hi][di]))

                    gest, conf = ('', 0)
                    if detect_gest:
                        raw_gest, raw_conf = classify_gesture(dofs, personal, pts)
                        gest, conf = gest_voters[hi].update(raw_gest, raw_conf)

                    detected.append({'dof': dofs, 'hand': side,
                                     'gest': gest, 'conf': conf})

                    _draw_skeleton(frame, lm)

            if not out_q.full():
                out_q.put_nowait({'hands': detected})
            if not frame_q.full():
                _, jpg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                frame_q.put_nowait(bytes(jpg))

        hands_detector.close()

    cap.release()


# ─────────────────────────────────────────────────────────────────────────────
#  MUJOCO WORKER PROCESS
# ─────────────────────────────────────────────────────────────────────────────
def _mujoco_process(mjcf_path: str,
                    angles_arr: mp.Array,
                    pixel_arr:  mp.Array,
                    ready_ev:   mp.Event,
                    stop_ev:    mp.Event,
                    ok_flag:    mp.Value,
                    sim_hz:     int = 1000,
                    render_hz:  int = 60):
    """
    1000 Hz simulation + offscreen render.
    Windows: uses WGL (default) — do NOT set MUJOCO_GL=egl.
    Linux:   uses EGL for headless rendering.
    """
    import sys as _sys
    if _sys.platform != 'win32':
        os.environ['MUJOCO_GL'] = 'egl'    # Linux/Mac headless
    else:
        # Windows: let MuJoCo pick WGL automatically
        os.environ.pop('MUJOCO_GL', None)
    import mujoco

    if not Path(mjcf_path).exists():
        print(f"[MuJoCo] XML not found: {mjcf_path}")
        ok_flag.value=-1; ready_ev.set(); return

    try:
        model = mujoco.MjModel.from_xml_path(mjcf_path)
        data  = mujoco.MjData(model)
        print(f"[MuJoCo] Model loaded: {model.nbody} bodies, {model.njnt} joints")
    except Exception as e:
        print(f"[MuJoCo] XML load error: {e}")
        ok_flag.value=-1; ready_ev.set(); return

    try:
        renderer = mujoco.Renderer(model, height=RENDER_H, width=RENDER_W)
        print(f"[MuJoCo] Renderer OK ({RENDER_W}×{RENDER_H})")
    except Exception as e:
        print(f"[MuJoCo] Renderer error: {e}")
        ok_flag.value=-1; ready_ev.set(); return

    # ── Camera ────────────────────────────────────────────────────────────
    cam = mujoco.MjvCamera()
    mujoco.mjv_defaultCamera(cam)
    cam.type      = mujoco.mjtCamera.mjCAMERA_FREE
    cam.distance  = 0.45
    cam.azimuth   = 140.0   # front-left view
    cam.elevation = -18.0
    cam.lookat[:] = np.array([0.0, 0.0, 0.05])

    # ── Detect hand side from XML path ────────────────────────────────────
    side = 'left' if 'left' in Path(mjcf_path).stem.lower() else 'right'
    S    = side

    # ── Build joint-name → qpos-address map ───────────────────────────────
    jmap: Dict[str, int] = {}
    for i in range(model.njnt):
        jmap[model.joint(i).name] = model.jnt_qposadr[i]

    # ── DOF → (joint_name, joint_max_rad) ─────────────────────────────────
    # Only proximal joints — PIP/2nd joints are coupled via XML equality.
    # Limits from the MJCF models:
    #   <finger>_prox_joint : 0..1.4381 rad  (proximal link, ~82 deg)
    #   thumb_base_joint    : 0..1.1641 rad  (rotation / abduction)
    #   thumb_mid_joint     : 0..0.5    rad  (main bend)
    DOF_MAP = [
        (0, f'{S}_pinky_prox_joint', 1.4381),   # Pinky
        (1, f'{S}_ring_prox_joint',   1.4381),   # Ring
        (2, f'{S}_middle_prox_joint', 1.4381),   # Middle
        (3, f'{S}_index_prox_joint',  1.4381),   # Index
        (4, f'{S}_thumb_mid_joint',  0.90),     # Thumb Bend (MCP flex)
        (5, f'{S}_thumb_base_joint',  1.1641),   # Thumb Rot  (CMC abduction)
    ]

    # Clamp to actual model joint ranges
    def _jrange_max(jname):
        for i in range(model.njnt):
            if model.joint(i).name == jname:
                return float(model.jnt_range[i, 1])
        return 0.0

    DOF_MAP_CLAMPED = [(dof, jn, min(jmax, _jrange_max(jn)))
                       for dof, jn, jmax in DOF_MAP]
    present = [jn for _, jn, _ in DOF_MAP_CLAMPED if jn in jmap]
    print(f"[MuJoCo] {side} hand — {model.njnt} joints, "
          f"{len(present)}/6 DOF joints mapped")

    def _apply(hw_vals):
        """
        hw_vals: list[6] in hardware convention (1000=open, 0=closed).
        Convert → joint angle: angle = (1 - hw/1000) * joint_max
        So 1000 (open)  → 0 rad  (extended)
           0    (closed) → max rad (fully bent)
        """
        for dof, jname, jmax in DOF_MAP_CLAMPED:
            if jname not in jmap or jmax < 1e-6: continue
            frac  = max(0.0, min(1.0, hw_vals[dof] / 1000.0))
            angle = (1.0 - frac) * jmax
            data.qpos[jmap[jname]] = angle

    mujoco.mj_forward(model, data)
    ok_flag.value = 1
    ready_ev.set()

    sim_dt    = 1.0 / sim_hz
    render_dt = 1.0 / render_hz
    last_render = 0.0

    while not stop_ev.is_set():
        t0 = time.perf_counter()

        with angles_arr.get_lock():
            ang = list(angles_arr[:])

        _apply(ang)
        mujoco.mj_step(model, data)

        now = time.perf_counter()
        if now - last_render >= render_dt:
            renderer.update_scene(data, camera=cam)
            px = renderer.render()
            flat = px.flatten()
            with pixel_arr.get_lock():
                pixel_arr[:] = flat
            last_render = now

        elapsed = time.perf_counter() - t0
        sleep_t  = sim_dt - elapsed
        if sleep_t > 0.0001:
            time.sleep(sleep_t)

    renderer.close()


# ─────────────────────────────────────────────────────────────────────────────
#  MODBUS WORKER PROCESS
# ─────────────────────────────────────────────────────────────────────────────
def _modbus_process(cfg: dict,
                    state_q:   mp.Queue,    # out: hand state dicts
                    cmd_q:     mp.Queue,    # in:  {'angles':[6],'forces':[6],'speeds':[6]}
                    stop_ev:   mp.Event):
    """
    Polls hand state at poll_hz, sends commands when cmd_q has items.
    cfg: {ip, port, dev_id, demo_mode, poll_hz}
    """
    import struct as _struct

    ip       = cfg.get('ip', '192.168.123.210')
    port     = cfg.get('port', 6000)
    dev_id   = cfg.get('dev_id', 1)
    demo     = cfg.get('demo_mode', True)
    poll_hz  = cfg.get('poll_hz', 100)
    poll_dt  = 1.0 / poll_hz
    t_demo   = 0.0

    REG_ANGLE_SET = 1486; REG_FORCE_SET = 1498; REG_SPEED_SET = 1522
    REG_ANGLE_ACT = 1546; REG_FORCE_ACT = 1582; REG_CURRENT   = 1594
    REG_ERROR     = 1606; REG_STATUS    = 1612; REG_TEMP      = 1618

    client = None
    if not demo:
        try:
            from pymodbus.client import ModbusTcpClient
            client = ModbusTcpClient(ip, port=port, timeout=2)
            if not client.connect():
                client = None; demo = True
        except Exception:
            demo = True

    def _rs(addr, n):
        r = client.read_holding_registers(addr, n, dev_id)
        if r.isError(): return [0]*n
        pk = _struct.pack('>'+n*'H', *r.registers)
        return list(_struct.unpack('>'+n*'h', pk))

    def _ws(addr, vs):
        pk   = _struct.pack('>'+len(vs)*'h', *vs)
        regs = list(_struct.unpack('>'+len(vs)*'H', pk))
        client.write_registers(addr, regs, dev_id)

    while not stop_ev.is_set():
        t0 = time.perf_counter()

        # Handle commands
        while not cmd_q.empty():
            try:
                cmd = cmd_q.get_nowait()
                if not demo and client:
                    if 'angles' in cmd: _ws(REG_ANGLE_SET, cmd['angles'])
                    if 'forces' in cmd: _ws(REG_FORCE_SET, cmd['forces'])
                    if 'speeds' in cmd: _ws(REG_SPEED_SET, cmd['speeds'])
            except Exception:
                pass

        # Poll state
        if demo:
            t_demo += poll_dt * 0.8
            a = [int(500+490*math.sin(t_demo+i*0.9)) for i in range(6)]
            f = [int(abs(700*math.sin(t_demo*1.3+i))) for i in range(6)]
            c = [int(abs(450*math.sin(t_demo*0.8+i))) for i in range(6)]
            t = [int(37+9*math.sin(t_demo*0.12+i)) for i in range(6)]
            s = {'angle':a,'force':f,'pos':[v*2 for v in a],
                 'current':c,'error':[0]*6,'status':[2]*6,'temp':t}
        else:
            try:
                a = _rs(REG_ANGLE_ACT, 6)
                s = dict(angle=a,
                         force  =_rs(REG_FORCE_ACT, 6),
                         pos    =_rs(REG_ANGLE_ACT, 6),
                         current=_rs(REG_CURRENT,   6),
                         error  =[0]*6,
                         status =[2]*6,
                         temp   =[37]*6)
            except Exception:
                s = None

        if s and not state_q.full():
            state_q.put_nowait(s)

        elapsed = time.perf_counter() - t0
        sleep_t  = poll_dt - elapsed
        if sleep_t > 0:
            time.sleep(sleep_t)

    if client:
        try: client.close()
        except: pass


# ─────────────────────────────────────────────────────────────────────────────
#  PROCESS MANAGER  — start/stop helpers used by Qt main process
# ─────────────────────────────────────────────────────────────────────────────
class WorkerManager:
    """Manages all worker processes and shared memory."""

    def __init__(self):
        # Shared memory
        n_px = RENDER_W * RENDER_H * 3
        self.mj_angles = mp.Array(ctypes.c_double, N_DOF)
        self.mj_pixels = mp.Array(ctypes.c_uint8,  n_px)
        self.mj_ready  = mp.Event()

        # Queues
        self.cam_out_q  = mp.Queue(maxsize=3)
        self.cam_frame_q= mp.Queue(maxsize=2)
        self.modbus_state_q = mp.Queue(maxsize=5)
        self.modbus_cmd_q   = mp.Queue(maxsize=10)

        # Stop events
        self.cam_stop   = mp.Event()
        self.mj_stop    = mp.Event()
        self.mb_stop    = mp.Event()

        # Process handles
        self._cam_proc  = None
        self._mj_proc   = None
        self._mb_proc   = None

    # ── Camera ────────────────────────────────────────────────────────────
    def start_camera(self, cfg: dict):
        self.cam_stop.clear()
        # Always create fresh queues so stale frames from previous session
        self.cam_out_q   = mp.Queue(maxsize=3)
        self.cam_frame_q = mp.Queue(maxsize=2)
        # On Linux, importing opencv-python points Qt at the Qt plugins bundled
        # inside cv2. The spawned worker re-imports the main script, which
        # creates a QApplication; with these inherited it loads cv2's
        # incompatible xcb plugin and aborts. The GUI process has already
        # initialised Qt, so dropping them here affects only the child.
        for _var in ("QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR"):
            os.environ.pop(_var, None)
        self._cam_proc = mp.Process(
            target=_camera_process,
            args=(cfg, self.cam_out_q, self.cam_frame_q, self.cam_stop),
            daemon=True, name="CameraProc")
        self._cam_proc.start()

    def stop_camera(self):
        self.cam_stop.set()
        if self._cam_proc and self._cam_proc.is_alive():
            # The loop may be blocked in cap.read() when the stop flag is
            # set: allow a short grace period for a clean release, then kill.
            # The OS reclaims the device either way.
            self._cam_proc.join(timeout=0.4)
            if self._cam_proc.is_alive():
                self._cam_proc.terminate()
                self._cam_proc.join(timeout=0.5)
        self._cam_proc = None

    # ── MuJoCo ────────────────────────────────────────────────────────────
    def start_mujoco(self, mjcf_path: str, sim_hz=1000, render_hz=60):
        self.mj_stop.clear(); self.mj_ready.clear()
        self._mj_ok = mp.Value('i', 0)   # 0=pending, 1=success, -1=error
        self._mj_proc = mp.Process(
            target=_mujoco_process,
            args=(mjcf_path, self.mj_angles, self.mj_pixels,
                  self.mj_ready, self.mj_stop, self._mj_ok, sim_hz, render_hz),
            daemon=False, name="MuJoCoProc")   # daemon=False: WGL needs window creation rights
        self._mj_proc.start()
        self.mj_ready.wait(timeout=25)         # wait for init (success or failure)
        return self._mj_ok.value == 1          # True only if init succeeded

    def stop_mujoco(self):
        self.mj_stop.set()
        if self._mj_proc and self._mj_proc.is_alive():
            self._mj_proc.join(timeout=3)
            if self._mj_proc.is_alive():
                self._mj_proc.terminate()
        self._mj_proc = None

    def set_mujoco_angles(self, angles: List[float]):
        with self.mj_angles.get_lock():
            self.mj_angles[:] = angles

    def get_mujoco_frame(self) -> Optional[np.ndarray]:
        with self.mj_pixels.get_lock():
            px = np.frombuffer(self.mj_pixels, dtype=np.uint8).copy()
        if px.max() == 0:
            return None
        return px.reshape(RENDER_H, RENDER_W, 3)

    # ── Modbus ────────────────────────────────────────────────────────────
    def start_modbus(self, cfg: dict):
        self.mb_stop.clear()
        self._mb_proc = mp.Process(
            target=_modbus_process,
            args=(cfg, self.modbus_state_q, self.modbus_cmd_q, self.mb_stop),
            daemon=True, name="ModbusProc")
        self._mb_proc.start()

    def stop_modbus(self):
        self.mb_stop.set()
        if self._mb_proc and self._mb_proc.is_alive():
            self._mb_proc.join(timeout=3)
            if self._mb_proc.is_alive():
                self._mb_proc.terminate()
        self._mb_proc = None

    def send_angles(self, angles: List[int]):
        if not self.modbus_cmd_q.full():
            self.modbus_cmd_q.put_nowait({'angles': angles})

    def stop_all(self):
        self.stop_camera()
        self.stop_mujoco()
        self.stop_modbus()
