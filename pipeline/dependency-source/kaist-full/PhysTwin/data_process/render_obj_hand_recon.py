# Verification video: per-frame DENSE 3D reconstruction of the OBJECT + (wrist-down)
# HAND, fused from all cameras' RGB-D using the seg masks, rendered over all frames.
# Object points keep their real color; hand points are tinted so the two are easy to
# tell apart. Rendered with the correct world-up (PhysTwin frame is z-down: table=z=0,
# object z<=0, physical up = -z) so the scene is upright.
#
# This is a CHECK video (not a deliverable npz). Shows the hand manipulating the
# object in 3D across the whole clip.
#
# Usage (phystwin env):
#   python data_process/render_obj_hand_recon.py --case ./data/different_types/ep_test \
#       --object_id 0 --hand_id 1 --orbit_deg 30

import os
import json
import pickle
import argparse

import numpy as np
import cv2

try:
    import open3d as o3d
except Exception:
    o3d = None

INVALID = 65535


def load_case(case):
    meta = json.load(open(f"{case}/metadata.json"))
    Ks = [np.array(k, float) for k in meta["intrinsics"]]
    c2ws = [np.array(m, float) for m in pickle.load(open(f"{case}/calibrate.pkl", "rb"))]
    return meta, Ks, c2ws


def lift(case, cam, K, c2w, oid, frame, zmin, zmax):
    dp = f"{case}/depth/{cam}/{frame}.npy"
    mp = f"{case}/mask/{cam}/{oid}/{frame}.png"
    if not (os.path.isfile(dp) and os.path.isfile(mp)):
        return np.zeros((0, 3)), np.zeros((0, 3))
    d = np.load(dp).astype(float)
    d[d >= INVALID] = 0
    d /= 1000.0
    H, W = d.shape
    m = cv2.imread(mp, 0) > 127
    cp = f"{case}/color/{cam}/{frame}.png"
    col = cv2.cvtColor(cv2.imread(cp), cv2.COLOR_BGR2RGB).astype(float) / 255.0
    xs, ys = np.meshgrid(np.arange(W), np.arange(H))
    ray = np.stack([xs.ravel(), ys.ravel(), np.ones(W * H)], 1) * d.ravel()[:, None]
    camp = ray @ np.linalg.inv(K).T
    z = camp[:, 2]
    sel = (z > zmin) & (z < zmax) & m.ravel()
    world = (c2w @ np.hstack([camp, np.ones((len(camp), 1))]).T).T[:, :3]
    return world[sel], col.reshape(-1, 3)[sel]


def look_at(eye, center, up):
    f = center - eye; f /= np.linalg.norm(f)
    s = np.cross(f, up); s /= np.linalg.norm(s)
    u = np.cross(s, f)
    w = np.eye(4); w[0, :3] = s; w[1, :3] = u; w[2, :3] = f
    w[:3, 3] = -w[:3, :3] @ eye
    return w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--object_id", type=int, default=0)
    ap.add_argument("--hand_id", type=int, default=1)
    ap.add_argument("--zmin", type=float, default=0.1)
    ap.add_argument("--zmax", type=float, default=2.0)
    ap.add_argument("--hand_tint", type=float, default=0.55, help="blend toward red for hand")
    ap.add_argument("--orbit_deg", type=float, default=30.0, help="total azimuth sweep over clip")
    ap.add_argument("--up_z", type=float, default=-1.0, help="camera up = [0,0,up_z]; flip sign if upside-down")
    ap.add_argument("--wh", type=int, nargs=2, default=[960, 720])
    args = ap.parse_args()

    case = args.case
    out = args.out or f"{case}/obj_hand_recon.mp4"
    meta, Ks, c2ws = load_case(case)
    N = meta["frame_num"]
    ncam = len(Ks)

    def fuse(frame):
        op, oc, hp, hc = [], [], [], []
        for ci in range(ncam):
            w, c = lift(case, ci, Ks[ci], c2ws[ci], args.object_id, frame, args.zmin, args.zmax)
            op.append(w); oc.append(c)
            w, c = lift(case, ci, Ks[ci], c2ws[ci], args.hand_id, frame, args.zmin, args.zmax)
            hp.append(w); hc.append(c)
        op = np.concatenate(op) if op else np.zeros((0, 3))
        oc = np.concatenate(oc) if oc else np.zeros((0, 3))
        hp = np.concatenate(hp) if hp else np.zeros((0, 3))
        hc = np.concatenate(hc) if hc else np.zeros((0, 3))
        if len(hc):  # tint hand toward red
            hc = hc * (1 - args.hand_tint) + np.array([1.0, 0.1, 0.1]) * args.hand_tint
        pts = np.concatenate([op, hp]) if len(op) + len(hp) else np.zeros((0, 3))
        cols = np.concatenate([oc, hc]) if len(oc) + len(hc) else np.zeros((0, 3))
        return pts, cols

    # camera placement from a representative (object) frame; physical up = -z
    pm, _ = fuse(N // 2)
    if len(pm) == 0:
        print("[err] no points at mid frame"); return
    center = np.median(pm, 0)
    extent = np.percentile(np.linalg.norm(pm - center, axis=1), 75) if len(pm) else 0.3
    dist = max(extent * 3.0, 0.6)
    up = np.array([0.0, 0.0, args.up_z])
    elev = np.deg2rad(25)

    W, H = args.wh
    vis = o3d.visualization.Visualizer()
    vis.create_window(visible=False, width=W, height=H)
    opt = vis.get_render_option(); opt.background_color = np.array([0.05, 0.05, 0.07]); opt.point_size = 3.0
    intr = o3d.camera.PinholeCameraIntrinsic(W, H, 0.9 * W, 0.9 * W, W / 2 - 0.5, H / 2 - 0.5)
    pc = o3d.geometry.PointCloud(); added = False
    vw = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), 30, (W, H))
    for f in range(N):
        pts, cols = fuse(f)
        if len(pts) == 0:
            continue
        pc.points = o3d.utility.Vector3dVector(pts)
        pc.colors = o3d.utility.Vector3dVector(np.clip(cols, 0, 1))
        if not added:
            vis.add_geometry(pc); added = True
        else:
            vis.update_geometry(pc)
        az = np.deg2rad(-args.orbit_deg / 2 + args.orbit_deg * f / max(N - 1, 1))
        eye = center + dist * np.array([np.cos(elev) * np.cos(az),
                                        np.cos(elev) * np.sin(az), -np.sin(elev)])
        cam = o3d.camera.PinholeCameraParameters()
        cam.intrinsic = intr; cam.extrinsic = look_at(eye, center, up)
        vis.get_view_control().convert_from_pinhole_camera_parameters(cam, allow_arbitrary=True)
        vis.poll_events(); vis.update_renderer()
        img = (np.clip(np.asarray(vis.capture_screen_float_buffer(True)), 0, 1) * 255).astype(np.uint8)
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        cv2.putText(img, f"f{f}  object(real) + hand(red)", (8, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        vw.write(img)
        if f % 30 == 0:
            print(f"  frame {f}/{N} pts={len(pts)}")
    vw.release()
    try:
        vis.destroy_window()
    except Exception:
        pass
    print(f"[done] {out}")


if __name__ == "__main__":
    main()
