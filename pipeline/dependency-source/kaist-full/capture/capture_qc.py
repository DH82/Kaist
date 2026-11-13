"""capture_qc.py — single PASS/WARN/FAIL gate for a multi-view RGB-D capture.

Adapted for our episode format:
  episode_dir/cam_N.mp4                    — color video
  episode_dir/cam_N_depth/frame_XXXXXX.npy — depth uint16 mm
  episode_dir/cam_N_intrinsics.json        — per-camera intrinsics
  episode_dir/extrinsics.json              — R, T (cam_1 as stereo reference)

Entry point: run_capture_qc(episode_dir) -> dict
"""

from __future__ import annotations

import glob
import itertools
import json
import os
from typing import Optional

import cv2
import numpy as np
import open3d as o3d

INVALID_U16 = 0
SAT_HI, SAT_LO = 250, 5

RECOMMENDATIONS: dict[str, dict[str, str]] = {
    "frame_continuity": {
        "FAIL": "한 개 이상의 카메라에서 프레임 드롭이 발생했습니다. USB 대역폭 또는 디스크 쓰기 속도를 확인하고 재수집하세요.",
        "WARN": "경미한 프레임 드롭이 있습니다. 재수집을 권장합니다.",
    },
    "color_quality": {
        "FAIL": "색상 품질이 심각하게 불량합니다. 조명 및 카메라 설정을 확인하세요.",
        "WARN": "일부 카메라의 노출 또는 선명도가 낮습니다. 조명 조건과 렌즈 오염을 확인하세요.",
    },
    "depth_completeness": {
        "FAIL": "깊이 데이터 누락이 심합니다. 피사체와 카메라 거리 또는 반사율을 확인하고 재수집하세요.",
        "WARN": "일부 깊이 데이터가 누락되어 3D 재구성 품질이 저하될 수 있습니다.",
    },
    "cross_cam_residual": {
        "FAIL": "카메라 간 포인트 클라우드 정렬이 불량합니다. 캘리브레이션을 재수행하세요.",
        "WARN": "카메라 간 정렬 편차가 다소 있습니다. 캘리브레이션 재확인을 권장합니다.",
    },
    "world_frame": {
        "FAIL": "테이블 평면을 인식할 수 없습니다. 카메라 배치 및 테이블 시야각을 확인하세요.",
        "WARN": "테이블 평면 인식에 문제가 있습니다.",
    },
    "sync": {
        "FAIL": "카메라 간 동기화 불량입니다. 하드웨어 트리거 연결을 확인하세요.",
        "WARN": "카메라 동기화에 경미한 문제가 있습니다. 고속 동작 구간의 3D 재구성 오류가 발생할 수 있습니다.",
    },
}

CHECK_LABELS: dict[str, str] = {
    "frame_continuity": "프레임 연속성",
    "color_quality": "색상 품질",
    "depth_completeness": "깊이 완전성",
    "cross_cam_residual": "카메라 간 잔차",
    "world_frame": "월드 좌표계",
    "sync": "동기화",
}


# ─────────────────────────────────────────────── data loading


def load_episode(episode_dir: str):
    """Load Ks, c2ws, cam_names, frame_indices from our episode format."""
    depth_dirs = sorted(
        d for d in os.listdir(episode_dir)
        if d.endswith("_depth") and os.path.isdir(os.path.join(episode_dir, d))
    )
    cam_names = [d[:-6] for d in depth_dirs]  # strip "_depth"

    Ks: list[np.ndarray] = []
    for cam in cam_names:
        ci = json.load(open(os.path.join(episode_dir, f"{cam}_intrinsics.json")))["color_intrinsics"]
        Ks.append(np.array([[ci["fx"], 0, ci["ppx"]], [0, ci["fy"], ci["ppy"]], [0, 0, 1]], float))

    # extrinsics.json: R,T from stereoCalibrate (P_camN = R @ P_cam1 + T)
    # cam->world: c2w = [R^T | -R^T @ T]
    extr = json.load(open(os.path.join(episode_dir, "extrinsics.json")))
    transforms = extr.get("transforms", extr)
    c2ws: list[np.ndarray] = []
    for cam in cam_names:
        t = transforms.get(cam, {})
        R = np.array(t.get("R", np.eye(3).tolist()), float)
        T = np.array(t.get("T", [[0], [0], [0]]), float).reshape(3, 1)
        c2w = np.eye(4)
        c2w[:3, :3] = R.T
        c2w[:3, 3] = (-R.T @ T).flatten()
        c2ws.append(c2w)

    depth_files = sorted(glob.glob(os.path.join(episode_dir, f"{cam_names[0]}_depth", "frame_*.npy")))
    frame_indices = [
        int(os.path.basename(f).replace("frame_", "").replace(".npy", ""))
        for f in depth_files
    ]
    return Ks, c2ws, cam_names, frame_indices


def _depth_m(episode_dir: str, cam: str, fi: int, zmax_raw: float = 20.0) -> np.ndarray:
    path = os.path.join(episode_dir, f"{cam}_depth", f"frame_{fi:06d}.npy")
    d = np.load(path).astype(np.float32)
    d[d == INVALID_U16] = 0
    d = d / 1000.0
    d[d > zmax_raw] = 0
    return d


# ─────────────────────────────────────────────── geometry helpers


def _ransac_plane(pts: np.ndarray, dist_thr: float = 0.008,
                  n_iter: int = 500, seed: int = 42):
    """고정 시드 RANSAC 평면 피팅 — 결정론적."""
    rng = np.random.default_rng(seed)
    best_plane = np.array([0.0, 0.0, 1.0, 0.0])
    best_inliers: list = []
    n = len(pts)
    for _ in range(n_iter):
        idx = rng.choice(n, 3, replace=False)
        p0, p1, p2 = pts[idx]
        normal = np.cross(p1 - p0, p2 - p0)
        norm = np.linalg.norm(normal)
        if norm < 1e-10:
            continue
        normal = normal / norm
        d = float(-normal @ p0)
        inl = np.where(np.abs(pts @ normal + d) < dist_thr)[0]
        if len(inl) > len(best_inliers):
            best_inliers = inl.tolist()
            best_plane = np.array([normal[0], normal[1], normal[2], d])
    return best_plane, best_inliers


def _grade(val: float, good: float, warn: float, higher_better: bool = False,
           tolerance: float = 0.0) -> str:
    """good 기준에서 tolerance 이내의 미달은 PASS로 처리 (비전공자 사용자 배려)."""
    if higher_better:
        return "PASS" if val >= (good - tolerance) else ("WARN" if val >= warn else "FAIL")
    return "PASS" if val <= (good + tolerance) else ("WARN" if val <= warn else "FAIL")


def _vox(pts: np.ndarray, v: float) -> np.ndarray:
    pc = o3d.geometry.PointCloud()
    pc.points = o3d.utility.Vector3dVector(pts)
    return np.asarray(pc.voxel_down_sample(v).points)


def _nn_med(a: np.ndarray, b: np.ndarray, thr: float):
    pc = o3d.geometry.PointCloud()
    pc.points = o3d.utility.Vector3dVector(b)
    kdt = o3d.geometry.KDTreeFlann(pc)
    ds = np.array([np.sqrt(kdt.search_knn_vector_3d(p, 1)[2][0]) for p in a])
    sh = ds < thr
    return sh.mean(), (np.median(ds[sh]) * 1000 if sh.sum() > 20 else float("nan"))


def _lift(episode_dir: str, K: np.ndarray, c2w: np.ndarray, cam: str, fi: int,
          zmin: float, zmax: float) -> np.ndarray:
    d = _depth_m(episode_dir, cam, fi)
    H, W = d.shape
    xs, ys = np.meshgrid(np.arange(W), np.arange(H))
    ray = np.stack([xs.ravel(), ys.ravel(), np.ones(W * H)], 1) * d.ravel()[:, None]
    camp = ray @ np.linalg.inv(K).T
    sel = (camp[:, 2] > zmin) & (camp[:, 2] < zmax)
    return (c2w @ np.hstack([camp, np.ones((len(camp), 1))]).T).T[:, :3][sel]


def _scene_clouds(episode_dir, Ks, c2ws, cam_names, fi, zmin, zmax, voxel, radius, drop_plane,
                  frame_override: Optional[dict] = None):
    """frame_override: {cam_idx: frame_idx} — 타임스탬프 기반 매칭 시 카메라별 다른 frame 사용."""
    cl: dict[int, np.ndarray] = {}
    for i, cam in enumerate(cam_names):
        actual_fi = frame_override[i] if frame_override and i in frame_override else fi
        try:
            cl[i] = _vox(_lift(episode_dir, Ks[i], c2ws[i], cam, actual_fi, zmin, zmax), voxel)
        except Exception:
            cl[i] = np.zeros((0, 3))

    valid = [p for p in cl.values() if len(p)]
    if not valid:
        return cl, np.zeros(3), None
    ctr = np.median(np.concatenate(valid, 0), 0)
    cl = {k: p[np.linalg.norm(p - ctr, axis=1) < radius] for k, p in cl.items()}

    plane = None
    if drop_plane:
        valid_after = [p for p in cl.values() if len(p)]
        if not valid_after:
            return cl, ctr, None
        all_pts = np.concatenate(valid_after, 0)
        if len(all_pts) > 100:
            plane, _ = _ransac_plane(all_pts)
            a, b, c, d = plane
            n = np.array([a, b, c])
            cl = {k: p[np.abs(p @ n + d) > 0.012] for k, p in cl.items()}
    return cl, ctr, plane


# ─────────────────────────────────────────────── checks


def _load_host_times(episode_dir: str, cam: str) -> Optional[list]:
    """cam_N_timestamps.csv에서 (frame_idx, host_time_s) 목록 반환. 없으면 None."""
    p = os.path.join(episode_dir, f"{cam}_timestamps.csv")
    if not os.path.exists(p):
        return None
    rows = []
    with open(p) as f:
        next(f)  # header
        for line in f:
            parts = line.strip().split(",")
            if len(parts) >= 3:
                rows.append((int(parts[0]), float(parts[2])))  # (frame_idx, host_time_s)
    return rows


def _check_frame_continuity(episode_dir, cam_names, frame_indices):
    counts: dict[str, tuple[int, int]] = {}
    gaps_per_cam: dict[str, int] = {}
    host_rows: dict[str, list] = {}

    for cam in cam_names:
        files = sorted(glob.glob(os.path.join(episode_dir, f"{cam}_depth", "frame_*.npy")))
        indices = [int(os.path.basename(f).replace("frame_", "").replace(".npy", "")) for f in files]
        cap = cv2.VideoCapture(os.path.join(episode_dir, f"{cam}.mp4"))
        nc = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cap.release()
        counts[cam] = (len(indices), nc)
        gaps_per_cam[cam] = int((np.diff(indices) != 1).sum()) if len(indices) > 1 else 0
        rows = _load_host_times(episode_dir, cam)
        if rows:
            host_rows[cam] = rows

    total_gaps = sum(gaps_per_cam.values())
    count_str = "  ".join(f"{c}:{counts[c][0]}d/{counts[c][1]}c" for c in cam_names)

    # host_time_s 기반 공통 윈도우 계산
    if len(host_rows) == len(cam_names):
        t_starts = {c: host_rows[c][0][1] for c in cam_names}
        t_ends   = {c: host_rows[c][-1][1] for c in cam_names}
        window_start = max(t_starts.values())
        window_end   = min(t_ends.values())
        overlap_s = window_end - window_start
        # 공통 윈도우 내 각 카메라 프레임 수
        win_counts = {
            c: sum(1 for _, t in host_rows[c] if window_start <= t <= window_end)
            for c in cam_names
        }
        win_str = "  ".join(f"{c}:{win_counts[c]}" for c in cam_names)
        detail = (
            f"per-cam: {count_str}  "
            f"overlap: {overlap_s:.1f}s  window-frames: {win_str}"
        )
        if overlap_s < 0:
            # 공통 구간 없음 — 녹화 윈도우가 겹치지 않음
            total_gaps += 99
            detail += "  WARNING: 공통 녹화 구간 없음"
    else:
        # 구버전 에피소드 (타임스탬프 없음) — 프레임 수로 대체
        min_depth = min(v[0] for v in counts.values()) if counts else 0
        detail = f"per-cam: {count_str}  shared-window: {min_depth}frames (no timestamps)"

    if total_gaps > 0:
        detail += "  dropped:" + " ".join(f"{c}:{gaps_per_cam[c]}" for c in cam_names if gaps_per_cam[c])

    status = "PASS" if total_gaps == 0 else ("WARN" if total_gaps <= 5 else "FAIL")
    return "frame_continuity", status, detail, "2D track / 3D recon"


def _check_color_quality(episode_dir, cam_names, sample_indices, sharp_rel_thr=0.4):
    bad_exp = 0
    blurs: list[tuple] = []
    brights: list[float] = []
    for cam in cam_names:
        cap = cv2.VideoCapture(os.path.join(episode_dir, f"{cam}.mp4"))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        cb: list[float] = []
        for fi in sample_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(fi, total - 1))
            ret, img = cap.read()
            if not ret or img is None:
                continue
            g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            mb = float(g.mean())
            brights.append(mb)
            sat = (g > SAT_HI).mean() + (g < SAT_LO).mean()
            if mb < 35 or mb > 220 or sat > 0.25:
                bad_exp += 1
            cb.append(cv2.Laplacian(g, cv2.CV_64F).var())
        cap.release()
        if cb:
            blurs.append((cam, float(np.median(cb)), float(min(cb))))

    med_sharp = float(np.median([b[1] for b in blurs])) if blurs else 0.0
    blur_flag = [c for c, med, _ in blurs if med < sharp_rel_thr * med_sharp]
    # bad_exp threshold: allow 1 bad frame per camera per 10 samples (transition frames)
    bad_exp_limit = max(2, len(cam_names))
    status = "WARN" if (bad_exp > bad_exp_limit or blur_flag) else "PASS"
    detail = (
        f"mean brightness {np.mean(brights):.0f}  exposure-bad frames {bad_exp}  "
        f"sharpness(median Laplacian var) {med_sharp:.0f}"
        + (f"  LOW-SHARP cams {blur_flag}" if blur_flag else "")
    )
    return "color_quality", status, detail, "2D seg / 2D track"


def _check_depth_completeness(episode_dir, cam_names, sample_indices, crop=0.6,
                               depth_dropout_pct=0.05,
                               depth_coverage_pass_pct=80.0,
                               depth_coverage_warn_pct=65.0,
                               depth_coverage_tolerance_pct=3.0,
                               depth_dropout_tolerance_pct=0.02):
    ratios: list[float] = []
    dropouts = 0
    total_samples = 0
    for cam in cam_names:
        counts: list[float] = []
        for fi in sample_indices:
            p = os.path.join(episode_dir, f"{cam}_depth", f"frame_{fi:06d}.npy")
            if not os.path.exists(p):
                continue
            d = _depth_m(episode_dir, cam, fi)
            H, W = d.shape
            y0 = int(H * (1 - crop) / 2); y1 = int(H * (1 + crop) / 2)
            x0 = int(W * (1 - crop) / 2); x1 = int(W * (1 + crop) / 2)
            counts.append(float((d[y0:y1, x0:x1] > 0).mean()))
        if counts:
            arr = np.array(counts)
            ratios.append(float(arr.mean()))
            dropouts += int((arr < 0.5 * np.median(arr)).sum())
            total_samples += len(counts)

    mean_ratio = float(np.mean(ratios)) * 100 if ratios else 0.0
    status = _grade(mean_ratio, depth_coverage_pass_pct, depth_coverage_warn_pct,
                    higher_better=True, tolerance=depth_coverage_tolerance_pct)
    dropout_limit = max(1, int(total_samples * depth_dropout_pct))
    dropout_tolerance = int(total_samples * depth_dropout_tolerance_pct)
    if dropouts > dropout_limit + dropout_tolerance and status == "PASS":
        status = "WARN"
    detail = (
        f"central valid-depth ratio {mean_ratio:.0f}%  "
        f"per-cam {['%.0f%%' % (r*100) for r in ratios]}  "
        f"dropout-frames {dropouts}/{total_samples} (limit {dropout_limit})"
    )
    return "depth_completeness", status, detail, "3D recon"


def _check_cross_cam_residual(episode_dir, Ks, c2ws, cam_names, sample_indices, voxel, radius, thr,
                               residual_warn_mm=15.0,
                               overlap_pass_pct=75.0, overlap_warn_pct=60.0,
                               overlap_comp_pct=85.0):
    meds: list[float] = []
    ovls: list[float] = []
    n = len(cam_names)
    for fi in sample_indices:
        cl, _, _ = _scene_clouds(episode_dir, Ks, c2ws, cam_names, fi, 0.2, 1.2, voxel, radius, False)
        for i, j in itertools.combinations(range(n), 2):
            if len(cl[i]) < 50 or len(cl[j]) < 50:
                continue
            o, m = _nn_med(cl[i], cl[j], thr)
            if not np.isnan(m):
                meds.append(m); ovls.append(o)
    med      = float(np.median(meds)) if meds else float("nan")
    mean_ovl = float(np.mean(ovls)) * 100 if ovls else 0.0

    residual_status = _grade(med, 5.0, residual_warn_mm)
    overlap_status  = _grade(mean_ovl, overlap_pass_pct, overlap_warn_pct, higher_better=True)
    _order = {"PASS": 0, "WARN": 1, "FAIL": 2}

    if residual_status == "FAIL":
        status = "FAIL"
    elif residual_status == "WARN" and mean_ovl >= overlap_comp_pct:
        status = "PASS"  # overlap이 보상 기준 이상일 때만 잔차 WARN 상쇄
    else:
        status = max([residual_status, overlap_status], key=lambda s: _order[s])

    detail = (
        f"median {med:.1f}mm  overlap {mean_ovl:.0f}%  (table as ref)  "
        f"residual: <5 PASS, <{residual_warn_mm:.0f} WARN  |  "
        f"overlap: >{overlap_pass_pct:.0f}% PASS (>{overlap_comp_pct:.0f}% 잔차보상)"
    )
    return "cross_cam_residual", status, detail, "3D recon / CALIB"


def _check_world_frame(episode_dir, Ks, c2ws, cam_names, fi, radius):
    cl, _, _ = _scene_clouds(episode_dir, Ks, c2ws, cam_names, fi, 0.2, 1.2, 0.01, radius, False)
    valid_pts = [p for p in cl.values() if len(p)]
    all_pts = np.concatenate(valid_pts, 0) if valid_pts else np.zeros((0, 3))
    if len(all_pts) < 100:
        return "world_frame", "FAIL", "포인트 클라우드 포인트 부족 (depth 데이터 확인 필요)", "3D recon / CALIB"
    plane, inl = _ransac_plane(all_pts)
    a, b, c, d = plane
    nv = np.array([a, b, c]); nv = nv / np.linalg.norm(nv)
    inl_ratio = len(inl) / len(all_pts)
    ang_z = float(np.degrees(np.arccos(min(1.0, abs(nv[2])))))
    table_z = float(-d / nv[2]) if abs(nv[2]) > 1e-3 else float("nan")
    centers = np.array([c2ws[i][:3, 3] for i in range(len(cam_names))])
    cams_consistent = len(set(np.sign(centers @ nv + d).tolist())) == 1
    plane_ok = inl_ratio > 0.25 and cams_consistent
    zup = ang_z < 8 and abs(table_z) < 0.05
    note = "z-up 완료" if zup else "reorient 전 상태 (납품 전 reorient 실행 필요)"
    detail = (
        f"table normal {np.round(nv,2)}  tilt-from-z {ang_z:.1f}deg  table_z {table_z:.3f}m  "
        f"inliers {inl_ratio*100:.0f}%  cams same-side {cams_consistent}  -> {note}"
    )
    return "world_frame", ("PASS" if plane_ok else "FAIL"), detail, "3D recon / CALIB"


def _check_sync(episode_dir, cam_names):
    """타임스탬프 CSV 직접 비교로 카메라 간 시간 오프셋 측정.

    cam_1 각 프레임의 host_time_s에 대해 나머지 카메라에서 가장 가까운
    프레임의 시간 차(ms)를 구해 mean/max를 보고한다.
    30fps = 프레임 간격 33ms이므로 mean < 10ms면 사실상 1프레임 이내 동기화.
    """
    import bisect

    all_ts: dict[str, list] = {}
    for cam in cam_names:
        rows = _load_host_times(episode_dir, cam)
        if rows:
            all_ts[cam] = sorted(rows, key=lambda r: r[1])

    if len(all_ts) < len(cam_names):
        missing = [c for c in cam_names if c not in all_ts]
        return "sync", "SKIP", f"타임스탬프 CSV 없음: {missing}", "3D recon"

    c0 = cam_names[0]
    c0_rows = all_ts[c0]

    # 공통 시간 창 계산
    t_starts = {c: rows[0][1] for c, rows in all_ts.items()}
    t_ends   = {c: rows[-1][1] for c, rows in all_ts.items()}
    win_start = max(t_starts.values())
    win_end   = min(t_ends.values())

    # 다른 카메라의 시간 배열 (이진탐색용)
    other_times: dict[str, list] = {
        cam: [r[1] for r in rows]
        for cam, rows in all_ts.items()
        if cam != c0
    }

    # cam_1 프레임 100개 샘플링 (창 안)
    c0_in_win = [r for r in c0_rows if win_start <= r[1] <= win_end]
    step = max(1, len(c0_in_win) // 100)
    sampled = c0_in_win[::step]

    offsets_ms: list[float] = []
    for _, t0 in sampled:
        for cam, times in other_times.items():
            idx = bisect.bisect_left(times, t0)
            best = float("inf")
            if idx < len(times):
                best = min(best, abs(times[idx] - t0))
            if idx > 0:
                best = min(best, abs(times[idx - 1] - t0))
            if best < float("inf"):
                offsets_ms.append(best * 1000.0)

    if not offsets_ms:
        return "sync", "SKIP", "공통 시간 창 내 프레임 없음", "3D recon"

    mean_ms = float(np.mean(offsets_ms))
    max_ms  = float(np.max(offsets_ms))
    status  = "PASS" if mean_ms < 16 else ("WARN" if mean_ms < 25 else "FAIL")
    detail  = (
        f"mean {mean_ms:.1f}ms  max {max_ms:.1f}ms  "
        f"(30fps=33ms/frame)  thresholds: <16ms PASS, <25ms WARN"
    )
    return "sync", status, detail, "3D recon"


# ─────────────────────────────────────────────── public API


def run_capture_qc(
    episode_dir: str,
    *,
    n_sample: int = 4,
    n_depth_sample: int = 8,
    voxel: float = 0.01,
    radius: float = 0.35,
    res_thr: float = 0.03,
    sharp_rel_thr: float = 0.4,
    depth_dropout_pct: float = 0.05,
    depth_coverage_pass_pct: float = 80.0,
    depth_coverage_warn_pct: float = 65.0,
    depth_coverage_tolerance_pct: float = 3.0,
    depth_dropout_tolerance_pct: float = 0.02,
    residual_warn_mm: float = 15.0,
    overlap_pass_pct: float = 75.0,
    overlap_warn_pct: float = 60.0,
    overlap_comp_pct: float = 85.0,
) -> dict:
    """Run all capture QC checks. Returns dict with overall verdict and per-check results."""
    Ks, c2ws, cam_names, frame_indices = load_episode(episode_dir)
    if not frame_indices:
        return {"ok": False, "error": "depth 프레임이 없습니다"}

    third = frame_indices[:max(1, len(frame_indices) // 3)]
    n = min(n_sample, len(third))
    static_fs = [int(third[int(i)]) for i in np.linspace(0, len(third) - 1, n)]
    nd = min(n_depth_sample, len(third))
    depth_fs = [int(third[int(i)]) for i in np.linspace(0, len(third) - 1, nd)]
    n_color = min(8, len(frame_indices))
    color_fs = [int(frame_indices[int(i)]) for i in np.linspace(0, len(frame_indices) - 1, n_color)]
    mid_static = static_fs[len(static_fs) // 2]

    raw = [
        _check_frame_continuity(episode_dir, cam_names, frame_indices),
        _check_color_quality(episode_dir, cam_names, color_fs, sharp_rel_thr=sharp_rel_thr),
        _check_depth_completeness(episode_dir, cam_names, depth_fs,
                                   depth_dropout_pct=depth_dropout_pct,
                                   depth_coverage_pass_pct=depth_coverage_pass_pct,
                                   depth_coverage_warn_pct=depth_coverage_warn_pct,
                                   depth_coverage_tolerance_pct=depth_coverage_tolerance_pct,
                                   depth_dropout_tolerance_pct=depth_dropout_tolerance_pct),
        _check_cross_cam_residual(episode_dir, Ks, c2ws, cam_names, static_fs, voxel, radius, res_thr,
                                   residual_warn_mm=residual_warn_mm,
                                   overlap_pass_pct=overlap_pass_pct,
                                   overlap_warn_pct=overlap_warn_pct,
                                   overlap_comp_pct=overlap_comp_pct),
        _check_world_frame(episode_dir, Ks, c2ws, cam_names, mid_static, radius),
        _check_sync(episode_dir, cam_names),
    ]

    order = {"SKIP": 0, "PASS": 0, "WARN": 1, "FAIL": 2}
    overall = max(raw, key=lambda r: order[r[1]])[1]

    checks = [
        {
            "name": name,
            "label": CHECK_LABELS.get(name, name),
            "status": status,
            "detail": detail,
            "gates": gates,
            "recommendation": RECOMMENDATIONS.get(name, {}).get(status),
        }
        for name, status, detail, gates in raw
    ]

    return {
        "ok": True,
        "overall": overall,
        "episode_id": os.path.basename(episode_dir),
        "frame_count": len(frame_indices),
        "cameras": cam_names,
        "checks": checks,
    }
