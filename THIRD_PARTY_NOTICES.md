# Third-party content

This project is licensed under GPL-3.0-or-later. That license applies to the
software written by the authors. It does **not** and cannot apply to
third-party material redistributed alongside it. Such material is listed
here.

---

## Hand geometry (`meshes/*.STL`)

**Origin.** The STL meshes describing the Inspire Hand RH56DFTP were obtained
directly from Inspire-Robots (Beijing Inspire Robots Technology Co., Ltd.),
the manufacturer of the hand, as part of the support package supplied with
the hardware. They are derived from the manufacturer's CAD data.

**What the authors changed.** The files were renamed to a descriptive scheme
(for example `little_force_sensor_2.STL` became
`pinky_force_sensor_nail.STL`). The geometry itself is unmodified.

**What the authors wrote.** The MJCF model files
(`inspire_hand_left.xml`, `inspire_hand_right.xml`) are the authors' own
work. The manufacturer's URDF and configuration files were used as the
kinematic reference, but the kinematic tree, joint limits, equality
constraints, actuators, contact exclusions, tactile sensor placement, scene
and visual settings were all authored here. These files are GPL-3.0.

**Licensing status: unresolved.** At the time of writing the authors have not
located an explicit license grant from Inspire-Robots covering redistribution
of the mesh files. The manufacturer's download portal
(<https://www.inspire-robots.com/support/download/dexterous%20hands/>)
distributes support material for hardware owners but does not, as far as the
authors could determine, attach license terms to the geometry.

Separately, the third-party repository `dexsuite/dex-urdf`
(<https://github.com/dexsuite/dex-urdf>) publishes Inspire hand models and
states CC BY-NC-SA 4.0 for parts of its content. That is a third party's
redistribution and does not govern the copy used here, which came from the
manufacturer. It is noted only because it is the most visible precedent.

**Two consequences worth understanding.**

1. **CC BY-NC-SA 4.0 is not compatible with GPL-3.0.** The non-commercial
   restriction conflicts with the GPL's guarantee of unrestricted use, and
   the two share-alike obligations cannot both be satisfied. So the meshes
   cannot simply be absorbed into this project's license; they are kept
   separate, as documented here.
2. **The authors claim no rights over the geometry.** All rights remain with
   Inspire-Robots. This notice records provenance; it is not a license grant,
   and the authors cannot give one.

**Recommended action for anyone redistributing this repository.** Confirm
terms with Inspire-Robots directly. Using the meshes with a hand you own is
the purpose for which the manufacturer supplies them; redistributing them is
a separate question and one only the manufacturer can answer.

**Removing them.** The software does not require these files. Everything
except the 3-D view runs without them, and the 3-D view reports a missing
model rather than failing. To ship without them, delete `meshes/` and
instruct users to obtain the files from the manufacturer; the MJCF files
reference meshes by filename from `meshdir="meshes"`, so no code change is
needed.

---

## Register map

The Modbus register layout implemented in `inspire_hand_controller.py` is
documented publicly by the manufacturer in the RH56 series user manual. An
implementation of a documented protocol is the authors' own work and is
covered by the project license.

---

## Python dependencies

Listed in `requirements.txt` and installed separately by the user. Each
carries its own license (PyQt5: GPL-3.0 / commercial; MuJoCo: Apache-2.0;
OpenCV: Apache-2.0; MediaPipe: Apache-2.0; scikit-learn: BSD-3-Clause; NumPy:
BSD-3-Clause; pyqtgraph: MIT). None are redistributed here.
