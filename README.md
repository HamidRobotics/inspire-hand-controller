# Dexterous Hand Controller

Dual-hand PyQt5 control, calibration, and teleoperation suite for the
**Inspire Hand RH56DFTP** anthropomorphic robotic hand.

- Real-time Modbus-TCP device control (raw socket, no vendor SDK)
- MuJoCo-based 3D kinematic visualization with tactile-sensor overlays
- Webcam-driven motion-capture teleoperation (MediaPipe hand tracking), with
  automatic left/right hand detection and no manual setup per session
- Trainable gesture recognition (KNN or Random Forest classifier)
- Simultaneous control of one or two hands
- Optional simulation-only mode (Control tab) for demos/teaching with no
  physical hand connected
- Dark and light UI themes

## Demonstration video
[![Watch the video](https://markdown-videos-api.jorgenkh.no/youtube/KWEZ5uRGfcQ)](https://youtu.be/KWEZ5uRGfcQ)

*A short demo video showing the app controlling the physical Inspire Hand
RH56DFTP across its main tabs.*

## Requirements

- Python 3.9+
- Windows 10/11 and Ubuntu 22.04 (both tested with two physical hands; see
  [Linux notes](#linux-notes))
- See `requirements.txt` for Python packages

```bash
pip install -r requirements.txt
```

## Running

```bash
python inspire_hand_controller.py
```

## Repository layout

```
inspire_hand_controller.py   Main application (GUI, device I/O, 3D view, gestures)
workers.py                   Background QThread workers (device polling, mocap, etc.)
calibration.py               Calibration & gesture-model training utilities
inspire_hand_left.xml        MuJoCo MJCF model, left hand
inspire_hand_right.xml       MuJoCo MJCF model, right hand
meshes/                      STL meshes referenced by both XML models
finger_icons/                UI icons for the gesture/calibration panels
```

## Hardware

Target device: Inspire Hand RH56DFTP (left & right, 6 actuated DOF each),
connected over Ethernet via Modbus TCP.

The controller commands the hand through six channels and reads its status
over the standard register layout, so it is expected to work with other
Inspire RH56-series hands that expose the same six-DOF interface. A matching
MuJoCo model file is needed for the 3-D view to show the correct geometry;
everything else (control, tactile display, teleoperation, logging) is
model-independent. Only the RH56DFTP has been tested on physical hardware.

### Data per hand

Each finger has two links driven by one motor; the thumb adds a rotation
motor. That gives **6 commanded DOF over 12 mechanically coupled joints**.

Tactile sensing is dense — **17 arrays, 1062 individual sensing points per
hand**, each reported as a 12-bit value (**0–4095**, 0 = no contact):

| Region | Grid | Points | Count |
|---|---|---|---|
| Fingertip | 3 x 3 | 9 | 5 fingers = 45 |
| Nail | 12 x 8 | 96 | 5 fingers = 480 |
| Pad | 10 x 8 | 80 | 4 fingers = 320 |
| Thumb mid-phalanx | 3 x 3 | 9 | 9 |
| Thumb pad | 12 x 8 | 96 | 96 |
| Palm | 14 x 8 | 112 | 112 |
| **Total** | | | **1062** |

Every polling cycle (20 ms interval by default) the app reads **1104 values per hand** —
1062 tactile plus 6 DOF x 7 fields (angle, force, position, current,
temperature, status, error) — and writes target angle, speed and force limit
per DOF.

Note on register addressing: each sensing point occupies one 16-bit Modbus
register, so the tactile block spans 2124 bytes (1062 registers) in the
device's address map. The point count, not the byte span, is the number of
physical sensing elements.

## Network setup

The hands are reached over Ethernet, so the PC must be on the **same subnet**
as the hand. Give your Windows or Linux interface a **static IP in the same
range as the hands**, with subnet mask **255.255.255.0**.

For example, if the hands are at `192.168.11.210` and `192.168.11.211`, set
the PC to something like `192.168.11.100 / 255.255.255.0`. A PC left on DHCP,
or on a different range, will not see the hands however correct the settings
in the app are.

Two hands must have **different** addresses on that subnet.

If each hand has its **own network adapter** on the same subnet, the
operating system cannot tell which hand is behind which adapter and may send
both hands' traffic through one of them, so one hand stops answering. Add a
host route per hand. On Windows, in an administrator PowerShell (adapter
names as shown by `Get-NetAdapter`):

```powershell
New-NetRoute -DestinationPrefix 192.168.123.210/32 -InterfaceAlias "Ethernet 2" -NextHop 0.0.0.0 -RouteMetric 1
New-NetRoute -DestinationPrefix 192.168.123.211/32 -InterfaceAlias "Ethernet 3" -NextHop 0.0.0.0 -RouteMetric 1
```

For Linux, see below. A single adapter with a small Ethernet switch avoids
the issue altogether.

On Windows laptops with two GPUs, assign `python.exe` to the
high-performance GPU (Settings → System → Display → Graphics), otherwise
the 3-D views may render on the integrated GPU.

### Linux notes

Tested on Ubuntu 22.04 with two hands on two USB-Ethernet adapters.

- **Two adapters on one subnet** need a host route per hand, otherwise
  traffic for both hands leaves through one adapter. With NetworkManager:
  `nmcli con modify "<connection>" ipv4.method manual ipv4.addresses
  192.168.123.100/24 ipv4.routes 192.168.123.210/32` (and `.101` / `.211`
  for the second adapter).
- **USB-Ethernet adapter driver.** Ubuntu binds many ASIX AX88179 adapters
  to `cdc_ncm`, which adds about 2 ms to every Modbus round trip and limits
  polling with tactile data to about 12 Hz. Check with
  `readlink /sys/class/net/<iface>/device/driver`. Selecting the adapter's
  vendor configuration (`ax88179_178a`) or CDC-ECM (`cdc_ether`) through its
  USB `bConfigurationValue` restores about 26 to 35 Hz.
- **Hybrid-graphics laptops** render on the integrated GPU by default. Start
  the app with
  `__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia python inspire_hand_controller.py`
  to use the NVIDIA GPU. If the frame rate stays low, the GPU may be idling
  at a low clock; a minimum clock (`sudo nvidia-smi -lgc <min>,<max>`)
  removes that limit.
- **Python from the deadsnakes PPA** also sees Ubuntu's system packages,
  which are built for the system Python. Install with
  `pip install --user --ignore-installed -r requirements.txt`.

## Motion capture and gesture: hand assignment

Left and right are detected automatically from the camera; there is nothing
to assign. The match follows **the hand you show to the camera**, not the
hand that happens to be connected.

So if you show your **left** hand while only the **right** hand is plugged
in, the 3-D view correctly shows the left model moving, but **no physical
hand moves** because no device is bound to that side. This is expected, not a
fault. Show the hand that matches a connected session.

## Calibration files: examples only

The `calibration_range.json`, `gesture_samples.json` and
`gesture_model.pkl` files in this repository are **examples from the authors'
setup**, recorded during development and provided so the app starts in a
working state.

They were recorded with one particular camera, one pair of hands and one
lighting arrangement, and the landmark-to-closure mapping depends on all
three. **They will not be optimal for you, and may work poorly.** Before
relying on motion capture or gesture recognition, redo the calibration in the
app, in your own environment:

- **Motion capture:** use the in-app range calibration so open and closed
  positions match your own hands.
- **Gestures:** collect your own labelled samples and retrain. A model
  trained on someone else's hands, camera and lighting generalises badly.

## Where files are saved

Screenshots taken with the toolbar button are saved **in the same directory
as the log file**, so captures and recorded data from a session stay
together.

## Reading and resizing the views

The live plots are interactive:

| Action | Control |
|---|---|
| Zoom both axes | **Mouse wheel** over the plot |
| Stretch one axis | Hold **right mouse button** and drag (sideways = time, up/down = value) |
| Pan | Hold **left mouse button** and drag |
| Restore auto-scaling | Small **A** button, bottom-left of the plot |

The time axis counts **seconds since the current connection was
established**, and restarts at zero each time you connect.

Every panel can be resized: drag the divider bars between the plot,
hand-view and tactile areas to give more room to whichever you are
watching. The window itself resizes and maximises as usual, and panel
sizes are remembered between sessions.

## Using it without a physical hand
Every tab except live device polling works with no hardware attached: the
3-D View tab loads either hand's MuJoCo model on its own, so you can inspect
joint ranges, self-collision, and the MCP/PIP coupling that underlies the
hand's underactuated fingers. The Control tab additionally has a
**Simulation-only mode** checkbox that drives the on-screen hand directly
from the sliders/presets with nothing sent to real hardware, for a fully
hands-on (pun intended) way to teach or learn dexterous-hand kinematics
without needing physical access to an RH56DFTP unit.

## Known limitations

- **Only one MuJoCo simulation per hand runs at a time**, even across
  different tabs. This is handled automatically (starting a simulation for
  a hand in one tab pauses that same hand's simulation elsewhere) to keep
  frame rate smooth, since each hand's simulation renders on the Qt main
  thread; it is expected behavior, not a bug, if a viewer you weren't
  actively using shows as stopped.
- **Background polling** uses a 20 ms (50 Hz) interval per connected hand
  by default (Settings tab). Reading the state alone reaches the full
  50 Hz; with the tactile arrays each cycle needs 24 sequential Modbus
  requests, and the hand's reply time limits the rate to about 30 Hz.
  End-to-end motion-capture teleoperation latency has not been benchmarked;
  it is expected to be dominated by webcam/USB capture and MediaPipe
  inference time rather than by this application.
- **The hand has no communication watchdog.** If the app stops sending
  commands or the connection is lost mid-motion, the hand completes the
  motion to its last commanded target and then holds that position.
- **MuJoCo render resolution** for the embedded 3-D views defaults to
  640x480 for performance; this is noticeably lower than MuJoCo's own
  standalone viewer. Choose a higher resolution in Settings → Performance
  (applies after a restart) for a sharper but more GPU/CPU-intensive
  embedded view.
- **CSV logging** (Log tab) records joint angles, forces, currents,
  temperatures and error flags, one row per hand for every eighth sample
  the interface receives. Tick *Include tactile arrays* to add all 1062
  tactile values to each row; the files grow to roughly 30 MB per hour per
  hand.

## License

GPL-3.0-or-later — see `LICENSE`.

**The hand meshes in `meshes/` are not covered by that license.** They were
obtained from Inspire-Robots, the hand manufacturer, and all rights remain
with them. They have been renamed for clarity but the geometry is unmodified.
See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for the full statement,
including what this means if you fork or redistribute this repository. The
MJCF model files that reference the meshes are the authors' own work and are
GPL-3.0.

The application runs without the meshes present; only the 3-D view needs
them.

## Citation

If you use this software in academic work, please cite the accompanying
SoftwareX paper (citation details to be added upon publication).

## Authors

- **Hamid Reza Saadati** — University of Duisburg-Essen; Aalen University —
  [ORCID 0009-0004-7550-3400](https://orcid.org/0009-0004-7550-3400)
- **Wojciech Kowalczyk** — University of Duisburg-Essen, Lotharstr. 1,
  D-47047 Duisburg, Germany —
  [ORCID 0000-0002-7390-2836](https://orcid.org/0000-0002-7390-2836)
- **Sebastian Feldmann** — Aalen University, Beethovenstr. 1, 73430 Aalen,
  Germany — [ORCID 0000-0001-6054-7292](https://orcid.org/0000-0001-6054-7292)
