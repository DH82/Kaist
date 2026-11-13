# Interactive SEGMENTATION EDITOR on SAM 3.1, modeled on the SAM web-demo interaction:
#   - click on the object and the mask appears IMMEDIATELY (SAM runs per click on the
#     current frame); left click = positive (is object), right click = negative (not)
#   - on-screen BUTTONS + a frame SLIDER (not keyboard-only), so navigation is explicit
#   - "Track" propagates the clicked object across a window; "Save" writes the masks
# Every mask comes from SAM 3.1 itself (add_prompt / propagate_in_video); we capture the
# clicks and persist the result into the PhysTwin masks, overwriting in place.
#
# PORTABLE: color frames come from color/{cam}/{f}.png if present, else the small
# color/{cam}.mp4 (so ~84MB/episode transfers). Masks overwrite mask/{cam}/{obj}/{f}.png.
# Needs the `sam3` env + a GPU (SAM loads on the first click).
#
# Usage (sam3 env, on a machine with a display):
#   conda run -n sam3 env HF_HOME=<cache> python data_process/sam_mask_refine.py \
#       --case <case_dir> --cam 1

import os
import json
import glob
import argparse
import tempfile

import numpy as np
import cv2
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.widgets import Button, Slider, TextBox


# ---- color frames (PNG dir OR mp4) + PhysTwin masks -------------------------------

class FrameSource:
    def __init__(self, case):
        self.case = case
        self._cache = {}

    def _decode(self, cam):
        if cam in self._cache:
            return self._cache[cam]
        p = f"{self.case}/color/{cam}.mp4"
        if not os.path.exists(p):
            return None
        print(f"  decoding color/{cam}.mp4 (first use)...", flush=True)
        cap = cv2.VideoCapture(p)
        fr = []
        while True:
            ok, im = cap.read()
            if not ok:
                break
            fr.append(im)
        cap.release()
        self._cache = {cam: np.asarray(fr)}
        return self._cache[cam]

    def bgr(self, cam, f):
        p = f"{self.case}/color/{cam}/{f}.png"
        if os.path.exists(p):
            return cv2.imread(p)
        arr = self._decode(cam)
        return None if arr is None or f >= len(arr) else arr[f]

    def rgb(self, cam, f):
        b = self.bgr(cam, f)
        return None if b is None else cv2.cvtColor(b, cv2.COLOR_BGR2RGB)


def mask_path(case, cam, oid, f):
    return f"{case}/mask/{cam}/{oid}/{f}.png"


def load_mask(case, cam, oid, f):
    p = mask_path(case, cam, oid, f)
    if not os.path.exists(p):
        return None
    m = cv2.imread(p, 0)
    return None if m is None else (m > 127)


# Episode-level fields the reviewer fills in here. They are deliberately SEPARATE keys from
# the pipeline's own "interaction"/derived hand type: "interaction" is the per-OBJECT
# boilerplate from data_process/episodes.py (identical for every episode of the same object)
# and finalize_delivery rewrites it on each delivery run, so anything typed here would not
# survive in it. The metadata spreadsheet reads these human_* keys.
HUMAN_FIELDS = ("human_interaction", "human_hand_type", "human_single_type",
                "human_contact_type")


def read_human(case, key):
    p = f"{case}/metadata.json"
    if not os.path.exists(p):
        return ""
    try:
        return str(json.load(open(p)).get(key) or "")
    except Exception:
        return ""


def write_human(case, key, value):
    """Write one human_* key into {case}/metadata.json. Episode-level, so it is saved the
    moment it changes -- NOT on the Save button, which stores one (camera, object) mask."""
    p = f"{case}/metadata.json"
    if not os.path.exists(p):
        return False
    meta = json.load(open(p))
    meta[key] = value
    json.dump(meta, open(p, "w"), indent=2, ensure_ascii=False)
    return True


def save_mask(case, cam, oid, f, mask):
    d = f"{case}/mask/{cam}/{oid}"
    os.makedirs(d, exist_ok=True)
    cv2.imwrite(mask_path(case, cam, oid, f), (mask.astype(np.uint8) * 255))


def scan_areas(case, cam, oid, n):
    areas = np.full(n, -1, np.int64)
    for f in range(n):
        p = mask_path(case, cam, oid, f)
        if os.path.exists(p):
            m = cv2.imread(p, 0)
            areas[f] = int((m > 127).sum()) if m is not None else 0
    return areas


def bad_frames(areas):
    pos = areas[areas > 0]
    med = np.median(pos) if len(pos) else 0
    thr = 0.3 * med
    return set(int(f) for f in range(len(areas))
               if areas[f] <= 0 or (med > 0 and areas[f] < thr))


# ---- SAM 3.1 -----------------------------------------------------------------------

class Sam:
    """SAM 3.1 point tracker (build_sam3_video_model, loaded from the cached sam3.1
    checkpoint — no download, stays sam3.1). One engine drives BOTH the live per-frame
    click (Edit: add points on a frame -> instant mask) AND Track (propagate the points
    across a window). To keep init_state fast we operate on SHORT sub-video windows
    (init_state on 30 frames ~0.3s) instead of the whole clip (which hangs)."""

    CKPT = os.environ.get("KP_SAM3_CKPT")                    # facebook/sam3 checkpoint (config.env)

    def __init__(self, wh, get_bgr):
        import torch
        from sam3.model_builder import build_sam3_video_model
        self.torch = torch
        self.W, self.H = int(wh[0]), int(wh[1])
        self.get_bgr = get_bgr
        m = build_sam3_video_model(checkpoint_path=self.CKPT, load_from_HF=False)
        self.p = m.tracker
        self.p.backbone = m.detector.backbone
        self.state = None
        self._tmp = None

    def _write(self, cam, f0, f1):
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False).name
        vw = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*"mp4v"), 30, (self.W, self.H))
        for f in range(f0, f1 + 1):
            img = self.get_bgr(cam, f)
            vw.write(img if img is not None else np.zeros((self.H, self.W, 3), np.uint8))
        vw.release()
        return tmp

    @staticmethod
    def _extract(obj_ids, video_res, obj_id):
        try:
            i = list(obj_ids).index(obj_id)
        except (ValueError, TypeError):
            i = 0
        mk = (video_res[i] > 0.0)
        mk = mk.cpu().numpy() if hasattr(mk, "cpu") else np.asarray(mk)
        while mk.ndim > 2:
            mk = mk[0]
        return mk.astype(bool)

    def _rel(self, points, labels):
        return (self.torch.tensor([[float(x) / self.W, float(y) / self.H] for x, y in points],
                                  dtype=self.torch.float32),
                self.torch.tensor([int(l) for l in labels], dtype=self.torch.int32))

    def open_frame(self, cam, f):
        """Load a 1-frame window at f for live per-frame clicking (fast)."""
        self.close_frame()
        self._tmp = self._write(cam, f, f)
        self.state = self.p.init_state(video_path=self._tmp, offload_video_to_cpu=True)

    def click(self, oid, points, labels):
        """Add all current clicks on the open frame -> mask (or None)."""
        pts, lab = self._rel(points, labels)
        out = self.p.add_new_points_or_box(inference_state=self.state, frame_idx=0,
                                           obj_id=int(oid), points=pts, labels=lab,
                                           clear_old_points=True)
        return self._extract(out[1], out[3], oid)

    def close_frame(self):
        self.state = None
        if self._tmp and os.path.exists(self._tmp):
            os.remove(self._tmp)
        self._tmp = None

    def propagate(self, cam, oid, f0, span, points, labels, reverse=False):
        """Seed the points at f0 and propagate FORWARD over [f0, f0+span] or BACKWARD over
        [f0-span, f0] -> {global_frame: mask}. Backward is for e.g. an overexposed start:
        seed on a clear later frame, propagate back into the washed-out frames."""
        a = max(0, f0 - span) if reverse else f0     # subvideo start
        b = f0 if reverse else f0 + span             # subvideo end (inclusive; _write clamps)
        seed = f0 - a                                # local index of the seeded frame
        tmp = self._write(cam, a, b)
        try:
            st = self.p.init_state(video_path=tmp, offload_video_to_cpu=True)
            pts, lab = self._rel(points, labels)
            self.p.add_new_points_or_box(inference_state=st, frame_idx=seed, obj_id=int(oid),
                                         points=pts, labels=lab)
            out = {}
            for res in self.p.propagate_in_video(st, start_frame_idx=seed,
                                                 max_frame_num_to_track=span, reverse=reverse,
                                                 propagate_preflight=True):
                out[a + int(res[0])] = self._extract(res[1], res[3], oid)
            return out
        finally:
            os.remove(tmp)


# ---- UI ---------------------------------------------------------------------------

class Editor:
    def __init__(self, case, cams, objs, wh, n, thresh, cases=None):
        self.cases = list(cases) if cases else [case]   # episodes to review in one session
        self.case_i = self.cases.index(case) if case in self.cases else 0
        self.case, self.cams = case, cams
        self.objs = objs                          # [(oid, name), ...] e.g. [(0,'pad'),(1,'hand')]
        self.oid = objs[0][0]
        self.W, self.H = int(wh[0]), int(wh[1])
        self.n, self.thresh = n, thresh
        self.src = FrameSource(case)
        self.ci, self.f = 0, 0
        self.editing = False
        self.edit_frame = -1                     # frame currently being edited (-1 = none)
        self.points, self.labels = [], []       # clicks on the current edit frame
        self.span = 30
        self.proposal = {}                        # {frame: mask} pending save (from click/track)
        self.sam = None
        self._im = None                           # persistent AxesImage (fast redraw)
        self._pts = []                            # click-marker artists to clear each draw
        self._syncing = False                     # guard against slider<->goto re-entry
        self._stash = {}                          # {(cam,oid): proposal dict} kept across switch
        self.erasing = False                      # manual erase mode (keep mask, trim a part)
        self.brush = 10
        self._painting = False
        self._pval = 0                            # 0 = erase, 1 = restore
        self._base_key = None                     # cache the decoded color frame across redraws
        self._base_img = None
        self.norm_on = False                      # exposure-normalization toggle (display + SAM)
        self._nt = {}                             # per-cam target mean (clip median), lazy
        self._dirty = False                       # coalesced-render flag
        self._anno = -1                           # frame currently seeded in the tracker state
        self.status = "ready — pick camera/object, press Edit, click the object"
        self.areas = scan_areas(case, self.cam, self.oid, n)
        self.bad = bad_frames(self.areas)
        self._build()
        self._render()                            # immediate first paint

    def ensure_sam(self):
        if self.sam is None:
            self.status = "loading SAM 3.1 tracker (first edit, ~10s)..."; self._render()
            self.fig.canvas.flush_events()
            self.sam = Sam((self.W, self.H), self._disp_bgr)   # SAM sees normalized frames too
        return self.sam

    # -- exposure normalization (display + SAM). Frames whose mean sits in the normal band
    # are returned BIT-EXACT (deadzone -> no contamination); only over/under-exposed frames
    # (e.g. auto-exposure warmup washing a frame to white) are gained toward the clip median.
    def _norm_target(self, cam):
        if cam not in self._nt:
            idx = np.linspace(0, max(self.n - 1, 0), min(self.n, 40)).astype(int)
            ms = []
            for f in idx:
                b = self.src.bgr(cam, int(f))
                if b is not None:
                    ms.append(float(b.mean()))
            self._nt[cam] = float(np.median(ms)) if ms else 105.0
        return self._nt[cam]

    def _normalize(self, arr, cam):
        t = self._norm_target(cam)
        m = max(float(arr.mean()), 1.0)
        g = t / m
        if 0.72 <= g <= 1.4:                       # deadzone: normal exposure -> untouched
            return arr
        g = float(np.clip(g, 0.2, 6.0))
        return np.clip(arr.astype(np.float32) * g, 0, 255).astype(np.uint8)

    def _disp_bgr(self, cam, f):
        b = self.src.bgr(cam, f)
        return b if (b is None or not self.norm_on) else self._normalize(b, cam)

    def toggle_norm(self, _=None):
        self.norm_on = not self.norm_on
        self._base_key = None                      # force recompute of the display base
        self.status = ("exposure-norm ON — over/under frames corrected, normal untouched"
                       if self.norm_on else "exposure-norm OFF")
        self.draw()

    def bake_cam(self, _=None):
        """WRITE the exposure-normalized frames to the color RGB data for the CURRENT camera:
        overwrite color/{cam}/{f}.png (only off-exposure frames actually change — deadzone
        leaves normal frames byte-identical) and re-encode color/{cam}.mp4 so CoTracker/recon
        also read the corrected RGB. Idempotent (re-baking a corrected frame is a no-op)."""
        cam = self.cam
        mp4 = f"{self.case}/color/{cam}.mp4"
        cap = cv2.VideoCapture(mp4)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        cap.release()
        self.status = f"baking normalized RGB for cam {cam}... (writing PNG + re-encoding mp4)"
        self._render(); self.fig.canvas.flush_events()
        os.makedirs(f"{self.case}/color/{cam}", exist_ok=True)
        changed, out = 0, []
        for f in range(self.n):
            b = self.src.bgr(cam, f)
            if b is None:
                out.append(np.zeros((self.H, self.W, 3), np.uint8)); continue
            nb = self._normalize(b, cam)
            if nb is not b:                        # off-exposure -> normalized -> persist PNG
                cv2.imwrite(f"{self.case}/color/{cam}/{f}.png", nb); changed += 1
            out.append(nb)
        vw = cv2.VideoWriter(mp4, cv2.VideoWriter_fourcc(*"mp4v"), fps, (self.W, self.H))
        for im in out:
            vw.write(im)
        vw.release()
        self.src = FrameSource(self.case)          # re-read the now-corrected color from disk
        self._base_key = None
        self.status = f"BAKED cam {cam}: {changed} off-exposure frames -> color PNG + mp4"
        self.draw()

    @property
    def cam(self):
        return self.cams[self.ci]

    @property
    def obj_name(self):
        return dict(self.objs).get(self.oid, str(self.oid))

    def _set_brush(self, d):
        self.brush = max(1, min(120, self.brush + d))
        self.status = f"brush={self.brush}"
        self.draw()

    def _end_edit(self):
        if self.sam:
            self.sam.close_frame()
        self.editing = False
        self.edit_frame = -1
        self._anno = -1
        self.points, self.labels = [], []

    def _reload(self):
        """Reload the per-(camera,object) review state after a switch."""
        self.areas = scan_areas(self.case, self.cam, self.oid, self.n)
        self.bad = bad_frames(self.areas)
        self._im = None                            # force a fresh imshow (content changed)
        self.draw()

    def switch_cam(self, c):
        if c == self.cam:
            return
        self._stash[(self.cam, self.oid)] = self.proposal   # keep unsaved edits per view
        self._end_edit()
        self.ci = self.cams.index(c)
        self.proposal = self._stash.get((self.cam, self.oid), {})
        self.status = f"camera {self.cam}"
        self._reload()

    def switch_obj(self, oid):
        if oid == self.oid:
            return
        self._stash[(self.cam, self.oid)] = self.proposal
        self._end_edit()
        self.oid = oid
        self.proposal = self._stash.get((self.cam, self.oid), {})
        self.status = f"editing {self.obj_name} (obj {self.oid})"
        self._reload()

    def switch_case(self, delta):
        """Jump to the prev/next episode WITHOUT relaunching (SAM stays loaded). Unsaved
        proposals are per-(cam,obj) buffers — save before switching or they are dropped."""
        if len(self.cases) < 2:
            self.status = "only one episode loaded"; self.draw(); return
        self.case_i = (self.case_i + delta) % len(self.cases)
        self._end_edit()
        self._stash = {}                              # stash is per-case; clear on case change
        self.case = self.cases[self.case_i]
        cams, objs, wh, n = _case_meta(self.case)
        self.cams, self.objs = cams, objs
        self.W, self.H = int(wh[0]), int(wh[1])
        self.n = n
        self.oid = objs[0][0]
        self.ci, self.f = 0, 0
        self.src = FrameSource(self.case)             # fresh decode cache for the new episode
        self.proposal = {}
        self._base_key = None
        self._sync_case_widgets()
        self.status = f"case [{self.case_i + 1}/{len(self.cases)}] {os.path.basename(self.case)}"
        self._reload()

    def _set_human(self, key, value):
        """Toggle an episode-level label. Clicking the active option clears it."""
        cur = read_human(self.case, key)
        new = "" if cur == value else value
        if not write_human(self.case, key, new):
            self.status = "metadata.json not found — not saved"; self.draw(); return
        if key == "human_hand_type" and new == "double":
            write_human(self.case, "human_single_type", "")   # double has no single side
        self._refresh_toggles()
        self.status = f"{key} = {new or '(cleared)'}  -> metadata.json"
        self.draw()

    def _refresh_toggles(self):
        """Selected option is highlighted; the derived hand type is shown for reference only."""
        if not hasattr(self, "tog"):
            return
        for (key, opt), b in self.tog.items():
            on = (read_human(self.case, key) == opt)
            b.label.set_fontweight("bold" if on else "normal")
            b.color = "#7ec8ff" if on else "0.85"
            b.hovercolor = "#a8daff" if on else "0.95"
            b.ax.set_facecolor(b.color)

    def _submit_interaction(self, text):
        # TextBox.set_val() fires on_submit too, so a programmatic reload on case switch would
        # write the value straight back. Only persist what a person actually typed.
        if getattr(self, "_tb_syncing", False):
            return
        text = (text or "").strip()
        ok = write_human(self.case, "human_interaction", text)
        self.status = (f"interaction saved -> metadata.json: {text!r}" if ok
                       else "metadata.json not found — interaction NOT saved")
        self.draw()

    def _sync_case_widgets(self):
        """After a case switch: retune the frame slider to the new length, relabel object
        buttons (pad/cloth/sponge differ per episode), refresh the window title."""
        if hasattr(self, "tb_inter"):
            self._tb_syncing = True
            try:
                self.tb_inter.set_val(read_human(self.case, "human_interaction"))   # per-episode
            finally:
                self._tb_syncing = False
        self._refresh_toggles()
        self.s_frame.valmax = max(1, self.n - 1)
        self.s_frame.ax.set_xlim(self.s_frame.valmin, self.s_frame.valmax)
        self._syncing = True
        self.s_frame.set_val(0)
        self._syncing = False
        for oid, name in self.objs:
            if oid in self.obj_btns:
                self.obj_btns[oid].label.set_text(f"{name} ({oid})")
        try:
            self.fig.canvas.manager.set_window_title(
                f"PhysTwin SAM mask editor — {os.path.basename(self.case)} "
                f"[{self.case_i + 1}/{len(self.cases)}]")
        except Exception:
            pass

    # -- layout
    def _wrap(self, lbl, fn):
        def h(ev):
            try:
                fn(ev)
            except Exception:
                import traceback
                tb = traceback.format_exc()
                with open("/tmp/editor_crash.log", "a") as fh:
                    fh.write(f"--- {lbl} ---\n{tb}\n")
                self.status = f"ERROR in {lbl}: {tb.strip().splitlines()[-1][:70]}"
                try:
                    self.draw()
                except Exception:
                    pass
        return h

    def _build(self):
        for _k in [k for k in plt.rcParams if k.startswith("keymap.")]:
            plt.rcParams[_k] = []                  # disable mpl default keys -> no conflicts
        self.fig = plt.figure(figsize=(13, 8.5))
        self.fig.canvas.manager.set_window_title(
            f"PhysTwin SAM mask editor — {os.path.basename(self.case)} "
            f"[{self.case_i + 1}/{len(self.cases)}]")
        self.ax = self.fig.add_axes([0.03, 0.355, 0.94, 0.61])
        self.ax.axis("off")
        self.fig.canvas.mpl_connect("button_press_event", self.on_click)
        self.fig.canvas.mpl_connect("motion_notify_event", self.on_motion)
        self.fig.canvas.mpl_connect("button_release_event", self.on_release)
        self.fig.canvas.mpl_connect("key_press_event", self.on_key)

        # row: camera + object/hand selectors
        self.cam_btns = {}
        x = 0.03
        for c in self.cams:
            b = Button(self.fig.add_axes([x, 0.300, 0.075, 0.04]), f"cam {c}")
            b.on_clicked(self._wrap(f"cam{c}", lambda ev, c=c: self.switch_cam(c)))
            self.cam_btns[c] = b
            x += 0.08
        self.obj_btns = {}
        x = max(x + 0.03, 0.42)
        for oid, name in self.objs:
            b = Button(self.fig.add_axes([x, 0.300, 0.13, 0.04]), f"{name} ({oid})")
            b.on_clicked(self._wrap(f"obj{oid}", lambda ev, o=oid: self.switch_obj(o)))
            self.obj_btns[oid] = b
            x += 0.14
        # episode (case) prev/next — jump between the loaded episodes without relaunching
        self.case_btns = []
        for lbl, d in [("◀ ep (p)", -1), ("ep ▶ (n)", +1)]:
            b = Button(self.fig.add_axes([max(x + 0.02, 0.80), 0.300, 0.085, 0.04]), lbl)
            b.on_clicked(self._wrap(lbl, lambda ev, d=d: self.switch_case(d)))
            self.case_btns.append(b)
            x = max(x + 0.02, 0.80) + 0.09

        self.s_frame = Slider(self.fig.add_axes([0.07, 0.245, 0.66, 0.03]),
                              "frame", 0, max(1, self.n - 1), valinit=0, valstep=1)
        self.s_frame.on_changed(lambda v: None if self._syncing else self.goto(int(v)))
        self.s_span = Slider(self.fig.add_axes([0.82, 0.245, 0.13, 0.03]),
                             "span", 1, 200, valinit=self.span, valstep=1)
        self.s_span.on_changed(lambda v: setattr(self, "span", int(v)))

        specs = [("◀◀ -10", self.prev10), ("◀ prev", self.prev), ("next ▶", self.next),
                 ("+10 ▶▶", self.next10), ("next BAD", self.nextbad),
                 ("Edit", self.toggle_edit), ("◀Trk (r)", self.track_back),
                 ("Trk▶ (t)", self.track), ("Erase", self.toggle_erase),
                 ("Expo (g)", self.toggle_norm), ("Norm→RGB (w)", self.bake_cam),
                 ("Save", self.save), ("Del", self.delete),
                 ("Clear", self.clear), ("Quit", self.quit)]
        self.btns = []
        x = 0.03
        w = (0.94 - 0.01 * (len(specs) - 1)) / len(specs)
        for label, cb in specs:
            b = Button(self.fig.add_axes([x, 0.135, w, 0.05]), label)
            b.on_clicked(self._wrap(label, cb))
            self.btns.append(b)
            x += w + 0.01

        # What this episode actually does, typed by the person reviewing the masks. Stored as
        # metadata.json "human_interaction" and reported in the metadata spreadsheet; the
        # per-object boilerplate in data_process/episodes.py is left untouched.
        self.tb_inter = TextBox(self.fig.add_axes([0.13, 0.012, 0.74, 0.040]),
                                "interaction ", initial=read_human(self.case, "human_interaction"))
        self.tb_inter.on_submit(self._submit_interaction)

        # Episode-level labels the reviewer sets. Written the moment they are clicked; the
        # Save button is per (camera, object) mask and must not gate these.
        self.tog = {}
        groups = [("human_hand_type",   ["single", "double"], 0.03),
                  ("human_single_type", ["right", "left"],    0.30),
                  ("human_contact_type",["sparse", "dense"],  0.57)]
        for key, opts, x0 in groups:
            for i, opt in enumerate(opts):
                b = Button(self.fig.add_axes([x0 + i * 0.075, 0.068, 0.07, 0.045]), opt)
                b.on_clicked(self._wrap(f"{key}={opt}",
                                        lambda ev, k=key, o=opt: self._set_human(k, o)))
                self.tog[(key, opt)] = b
        self._refresh_toggles()
        # coalesce redraws: input handlers mark dirty; this timer renders the latest at
        # ~25fps. So navigating (even fast) never backs up the single-threaded event loop.
        self._timer = self.fig.canvas.new_timer(interval=40)
        self._timer.add_callback(self._tick)
        self._timer.start()

    # -- drawing (coalesced)
    def draw(self):
        self._dirty = True

    def _tick(self):
        if self._dirty:
            self._dirty = False
            self._render()

    def _render(self):
        if self._base_key != (self.cam, self.f, self.norm_on):   # cache -> cheap redraws
            img = self.src.rgb(self.cam, self.f)
            base = np.zeros((self.H, self.W, 3), np.uint8) if img is None else img
            if self.norm_on and img is not None:
                base = self._normalize(base, self.cam)
            self._base_img = base
            self._base_key = (self.cam, self.f, self.norm_on)
        ov = self._base_img.astype(np.float32)
        m = self.proposal.get(self.f)
        if m is not None:
            ov[m] = 0.45 * ov[m] + 0.55 * np.array([255, 220, 0])       # yellow: unsaved
            tag = f"EDIT {int(m.sum())}px"
        else:
            dm = load_mask(self.case, self.cam, self.oid, self.f)
            if dm is not None:
                ov[dm] = 0.5 * ov[dm] + 0.5 * np.array([0, 230, 0])     # green: saved
            tag = "empty" if dm is None else f"{int(dm.sum())}px"
        comp = np.clip(ov, 0, 255).astype(np.uint8)
        if self._im is None:                       # reuse one AxesImage -> fast redraw
            self._im = self.ax.imshow(comp)
        else:
            self._im.set_data(comp)
        for p in self._pts:                        # refresh click markers only
            p.remove()
        self._pts = []
        if self.f == self._edit_frame_or(-1):
            for (x, y), l in zip(self.points, self.labels):
                self._pts.append(self.ax.plot(x, y, "o", ms=11,
                                 color=("lime" if l == 1 else "red"), mec="black", mew=1.5)[0])
        flag = "  [BAD]" if self.f in self.bad else ""
        mode = (f"ERASE brush={self.brush}" if self.erasing
                else "EDIT (click)" if self.editing else "view")
        self.ax.set_title(f"cam {self.cam} · {self.obj_name}({self.oid})   "
                          f"frame {self.f}/{self.n-1}   [{tag}]{flag}   {mode}   |  {self.status}",
                          fontsize=11)
        self.fig.canvas.draw_idle()

    def _edit_frame_or(self, default):
        return self._anno if self.editing else default

    # -- navigation
    def goto(self, f):
        f = max(0, min(self.n - 1, int(f)))
        self.f = f
        if int(self.s_frame.val) != f:
            self._syncing = True                   # avoid slider->goto re-entry (double draw)
            self.s_frame.set_val(f)
            self._syncing = False
        self.draw()

    def prev(self, _=None): self.goto(self.f - 1)
    def next(self, _=None): self.goto(self.f + 1)
    def prev10(self, _=None): self.goto(self.f - 10)
    def next10(self, _=None): self.goto(self.f + 10)

    def nextbad(self, _=None):
        for f in range(self.f + 1, self.n):
            if f in self.bad:
                self.goto(f); return
        self.status = "no more BAD frames after here"; self.draw()

    # -- editing
    def toggle_edit(self, _=None):
        self.editing = not self.editing
        if self.editing:
            self.erasing = False                # modes are mutually exclusive
            self._painting = False
            self.points, self.labels = [], []
            self.ensure_sam()
            self.sam.open_frame(self.cam, self.f)
            self._anno = self.f
            self.status = "click the object (L=+ / R=-), then Track to propagate"
        else:
            self._end_edit()
            self.status = "view"
        self.draw()

    def on_key(self, ev):
        actions = {
            "right": self.next, "d": self.next, "left": self.prev, "a": self.prev,
            ".": self.next10, ",": self.prev10, "b": self.nextbad,
            "e": self.toggle_edit, "t": self.track, "s": self.save,
            "x": self.delete, "c": self.clear, "q": self.quit, "v": self.toggle_erase,
            "o": lambda _=None: self.switch_obj(0), "h": lambda _=None: self.switch_obj(1),
            "[": lambda _=None: self._set_brush(-4), "]": lambda _=None: self._set_brush(+4),
            "n": lambda _=None: self.switch_case(+1), "p": lambda _=None: self.switch_case(-1),
            "g": self.toggle_norm, "r": self.track_back, "w": self.bake_cam,
        }
        for c in self.cams:                        # digit keys 0-3 = cameras
            actions[str(c)] = (lambda _=None, c=c: self.switch_cam(c))
        fn = actions.get(ev.key)
        if fn:
            self._wrap(f"key '{ev.key}'", fn)(None)

    def toggle_erase(self, _=None):
        """Manual erase: keep the EXISTING mask, brush away (or restore) parts by dragging.
        Left drag = remove, right drag = restore. Nothing is re-segmented."""
        self.erasing = not self.erasing
        if self.erasing:
            self._end_edit()                          # leave SAM mode
            if self.f not in self.proposal:
                dm = load_mask(self.case, self.cam, self.oid, self.f)
                self.proposal[self.f] = (dm.copy() if dm is not None
                                         else np.zeros((self.H, self.W), bool))
            self.status = f"ERASE: drag L=remove R=restore  brush={self.brush} ([ ] to size)"
        else:
            self.status = "view"
        self.draw()

    def _paint(self, x, y):
        m = self.proposal.get(self.f)
        if m is None:
            dm = load_mask(self.case, self.cam, self.oid, self.f)
            m = dm.copy() if dm is not None else np.zeros((self.H, self.W), bool)
        mm = m.astype(np.uint8)
        cv2.circle(mm, (int(x), int(y)), self.brush, int(self._pval), -1)
        self.proposal[self.f] = mm.astype(bool)
        self.draw()

    def on_motion(self, ev):
        if self._painting and ev.inaxes == self.ax and ev.xdata is not None:
            self._paint(ev.xdata, ev.ydata)

    def on_release(self, ev):
        self._painting = False

    def on_click(self, ev):
        if ev.inaxes != self.ax or ev.xdata is None:
            return
        if self.erasing:                              # manual erase/restore stroke
            self._painting = True
            self._pval = 0 if ev.button == 1 else 1   # left=remove, right=restore
            self._paint(ev.xdata, ev.ydata)
            return
        if not self.editing:
            return
        if self.f != self._anno:                      # moved to a new frame -> fresh window
            self.sam.open_frame(self.cam, self.f)
            self._anno = self.f
            self.points, self.labels = [], []
        self.points.append((ev.xdata, ev.ydata))
        self.labels.append(1 if ev.button == 1 else 0)
        self.status = "segmenting..."; self._render(); self.fig.canvas.flush_events()
        m = self.sam.click(self.oid, self.points, self.labels)
        if m is not None and m.any():
            self.proposal[self.f] = m
            self.status = f"mask {int(m.sum())}px — Track to propagate, Save to keep"
        else:
            self.status = "no mask from those clicks — try another point"
        self.draw()

    def track(self, _=None):
        self._track(reverse=False)

    def track_back(self, _=None):
        self._track(reverse=True)

    def _track(self, reverse=False):
        if not self.editing or self._anno < 0 or not self.points:
            self.status = "Edit + click a frame first, then Track"; self.draw(); return
        span = min(self._anno, self.span) if reverse else min(self.n - 1 - self._anno, self.span)
        if span <= 0:
            self.status = f"no frames to track {'backward' if reverse else 'forward'}"
            self.draw(); return
        arrow = f"-{span}" if reverse else f"+{span}"
        self.status = f"tracking {self._anno} -> {arrow} ..."; self._render()
        self.fig.canvas.flush_events()
        out = self.sam.propagate(self.cam, self.oid, self._anno, span, self.points,
                                 self.labels, reverse=reverse)
        n = 0
        for f, m in out.items():
            if m is not None and m.any():
                self.proposal[f] = m; n += 1
        with open("/tmp/editor_track.log", "a") as _f:   # diagnostic
            _f.write(f"anno={self._anno} cam={self.cam} oid={self.oid} span={span} rev={reverse} "
                     f"npts={len(self.points)} out_frames={len(out)} nonempty={n} "
                     f"areas={[int(v.sum()) for v in list(out.values())[:6]]}\n")
        # Track is a one-shot op: next click re-anchors -> back to frame-by-frame editing
        self._anno = -1; self.points, self.labels = [], []
        self.status = f"tracked {n} frames ({arrow}) — scrub to review, Save"
        self.draw()

    def save(self, _=None):
        if not self.proposal:
            self.status = "nothing to save"; self.draw(); return
        for f, m in self.proposal.items():
            save_mask(self.case, self.cam, self.oid, f, m)
            self.areas[f] = int(m.sum())
            (self.bad.discard if self.areas[f] > 0 else self.bad.add)(f)
        self.status = f"saved {len(self.proposal)} masks to mask/{self.cam}/{self.oid}/"
        self.proposal = {}; self.points, self.labels = [], []
        self.draw()

    def delete(self, _=None):
        """Remove the object mask on THIS frame entirely (frame becomes empty). For when
        SAM masked something that shouldn't be there at all. Deletes the saved PNG."""
        self.proposal.pop(self.f, None)
        p = mask_path(self.case, self.cam, self.oid, self.f)
        removed = os.path.exists(p)
        if removed:
            os.remove(p)
        self.areas[self.f] = -1
        self.bad.add(self.f)
        self.points, self.labels = [], []
        self.status = f"deleted mask on frame {self.f} (now empty)" if removed \
            else f"frame {self.f} already had no mask"
        self.draw()

    def clear(self, _=None):
        self.points, self.labels = [], []
        self.proposal.pop(self.f, None)
        self.status = "cleared unsaved edits on this frame"; self.draw()

    def quit(self, _=None):
        if self.sam:
            self.sam.close_frame()
        plt.close(self.fig)


def _meta(case, wh, n):
    p = f"{case}/metadata.json"
    if os.path.exists(p):
        m = json.load(open(p)); wh = wh or m.get("WH"); n = n or m.get("frame_num")
    if wh is None or n is None:
        mp4 = sorted(glob.glob(f"{case}/color/*.mp4"))[0]
        cap = cv2.VideoCapture(mp4)
        n = n or int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        wh = wh or [int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))]
        cap.release()
    return wh, n


def _objs(case, cam):
    """[(obj_id, name)] from mask/mask_info_{cam}.json, else object+hand default."""
    p = f"{case}/mask/mask_info_{cam}.json"
    if os.path.exists(p):
        info = json.load(open(p))
        return [(int(k), v) for k, v in sorted(info.items(), key=lambda kv: int(kv[0]))]
    return [(0, "object"), (1, "hand")]


def _case_meta(case):
    """(cams, objs, wh, n) for a case dir — used at launch and on in-session case switch."""
    wh, n = _meta(case, None, None)
    cams_png = sorted(int(os.path.basename(d)) for d in glob.glob(f"{case}/color/*")
                      if os.path.isdir(d))
    cams_mp4 = sorted(int(os.path.basename(p)[:-4]) for p in glob.glob(f"{case}/color/*.mp4"))
    cams = cams_png or cams_mp4
    return cams, _objs(case, cams[0]), wh, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, nargs="+",
                    help="one or more case dirs; switch between them in-session (n/p keys or ep buttons)")
    ap.add_argument("--cam", default="all", help="'all' (default) or a camera index")
    ap.add_argument("--output_prob_thresh", type=float, default=0.5)
    ap.add_argument("--wh", type=int, nargs=2)
    ap.add_argument("--frame_num", type=int)
    args = ap.parse_args()
    cases = args.case
    first = cases[0]
    wh, n = _meta(first, args.wh, args.frame_num)
    cams_png = sorted(int(os.path.basename(d)) for d in glob.glob(f"{first}/color/*")
                      if os.path.isdir(d))
    cams_mp4 = sorted(int(os.path.basename(p)[:-4]) for p in glob.glob(f"{first}/color/*.mp4"))
    avail = cams_png or cams_mp4
    cams = avail if args.cam == "all" else [int(args.cam)]
    objs = _objs(first, cams[0])
    print(f"cases({len(cases)})={[os.path.basename(c) for c in cases]}")
    print(f"case={first} cams={cams} objs={objs} frames={n} WH={wh}")
    Editor(first, cams, objs, wh, n, args.output_prob_thresh, cases=cases)
    plt.show()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        with open("/tmp/editor_crash.log", "a") as fh:
            fh.write("--- STARTUP ---\n" + tb + "\n")
        raise
