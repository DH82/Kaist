# SAM 3.1 segmentation for PhysTwin: an OBJECT and a CONTROLLER, each emitted as
# a single mask per camera in PhysTwin format:
#
#   mask/{cam}/{obj_id}/{frame}.png      binary mask (0/255)
#   mask/mask_info_{cam}.json            {obj_id: label}   -> {0: object, 1: controller}
#
# Each "group" (object, controller) can be specified by MULTIPLE synonym prompts
# that are UNIONed into one mask. This matters because SAM 3.1's text detection is
# view-dependent: a manipulating limb reads as "hand" when the hand/fingers face a
# camera, but as "arm" when it rises into frame from below (the hand shape isn't
# clear). Prompting the controller with both "hand" and "arm" and unioning catches
# it in every view (verified: a view with hand=0 frames yields arm=111 frames).
#
# All instances of all prompts in a group are unioned -> one obj_id per group, so
# output is consistent across cameras ({0: object, 1: controller}), matching what
# PhysTwin's downstream expects (controller label e.g. "hand").
#
# Runs in the `sam3` conda env (sam3.1 branch). On Blackwell/RTX 5090 keep FA3 off.
#
# The controller(hand) prompts come from a SCENE preset (--scene_type) keyed by the
# manipulation dimensionality: 1d (rope), 2d (cloth) -> hand visible; 3d (lifting a
# volumetric object) -> hand occluded. See SCENE_PRESETS below. Object prompts stay explicit.
#
# Usage:
#   # cloth folding on a table (2d, hand visible):
#   $KP_PY_SAM3 data_process/segment_object_sam3.py \
#       --base_path ./data/different_types --case_name ep_20260630_014921 \
#       --object_prompts "blue cloth" --object_label cloth --scene_type 2d
#   # lifting a plush (3d, hand occluded):
#   ... --object_prompts "teddy bear" --scene_type 3d

import os
import glob
import json
import shutil
import argparse
from collections import defaultdict

import numpy as np
import cv2
import torch
from PIL import Image
from tqdm import tqdm

from sam3.model_builder import build_sam3_predictor


def clean_mask(m, min_area, close, ratio=0.0):
    """Morphological close (heal subtraction gaps) + drop connected components that
    are smaller than min_area OR smaller than ratio*largest (removes detached forearm
    slivers / 1-frame clothing fragments left by the subtract, while keeping the hand
    body even if it splits into a couple of pieces)."""
    mm = m.astype(np.uint8)
    if close > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * close + 1, 2 * close + 1))
        mm = cv2.morphologyEx(mm, cv2.MORPH_CLOSE, k)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mm, 8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    big = int(areas.max()) if len(areas) else 0
    out = np.zeros(mm.shape, bool)
    for i in range(1, n):
        a = stats[i, cv2.CC_STAT_AREA]
        if a >= min_area and a >= ratio * big:
            out[lab == i] = True
    return out


def _frame_masks(out):
    """Union one propagated frame's per-object binary masks into a CPU bool array.
    Converting off-GPU here (per frame) keeps GPU memory from accumulating over the
    whole clip (the old code held every frame's GPU tensors in a dict -> OOM)."""
    m = None
    masks = out["out_binary_masks"]
    for idx in range(len(out["out_obj_ids"])):
        mk = np.asarray(masks[idx]).astype(bool)
        if mk.ndim == 3:
            mk = mk[0]
        m = mk if m is None else (m | mk)
    return m


def _propagate(predictor, sid):
    per_frame = {}
    for r in predictor.handle_stream_request(
        request=dict(type="propagate_in_video", session_id=sid)
    ):
        m = _frame_masks(r["outputs"])
        if m is not None:
            per_frame[r["frame_index"]] = m
    return per_frame


def _safe_propagate(predictor, sid):
    """Propagate, but if the concept/points matched NOTHING in this (sub)video SAM raises
    'No points/prompts provided'. An entire 300-frame chunk can legitimately contain no
    instance of the object on one camera (fully occluded / rolled out of view), so treat
    that as an empty result (no masks for those frames -> left for human review) instead
    of crashing the whole multi-camera run."""
    try:
        return _propagate(predictor, sid)
    except RuntimeError as e:
        if "No points" in str(e) or "No prompts" in str(e):
            return {}
        raise


def union_concept(predictor, sid, concept, frame_idx, first, thresh=0.5):
    """Run one text concept over the loaded (sub)video -> {frame: union_mask(bool)}."""
    if not first:
        predictor.handle_request(request=dict(type="reset_session", session_id=sid))
    predictor.handle_request(request=dict(
        type="add_prompt", session_id=sid, frame_index=frame_idx, text=concept,
        output_prob_thresh=thresh))
    return _safe_propagate(predictor, sid)


def session_union(predictor, sid, prompts, first, thresh=0.5):
    """Union a group's synonym text prompts over the loaded (sub)video."""
    grp = {}
    for concept in prompts:
        pf = union_concept(predictor, sid, concept, 0, first, thresh)
        first = False
        for f, m in pf.items():
            grp[f] = m if f not in grp else (grp[f] | m)
    return grp, first


def sample_mask_points(mask, n=6):
    """Sample up to n foreground points spread across a boolean mask, as [x,y] for SAM."""
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return []
    idx = np.linspace(0, len(ys) - 1, min(n, len(ys))).astype(int)
    return [[int(xs[i]), int(ys[i])] for i in idx]


def point_union(predictor, sid, points, first, thresh=0.5):
    """Point-prompt (positive clicks) at frame 0 of the loaded (sub)video, then track.
    Used to CARRY the object across chunks: seed the next chunk with points sampled from
    the previous chunk's last object mask, so tracking stays continuous through hard
    deformation (e.g. a pad rolled into a cylinder) instead of re-detecting from text."""
    if not first:
        predictor.handle_request(request=dict(type="reset_session", session_id=sid))
    predictor.handle_request(request=dict(
        type="add_prompt", session_id=sid, frame_index=0, obj_id=0, points=points,
        point_labels=[1] * len(points), output_prob_thresh=thresh))
    return _safe_propagate(predictor, sid)


def write_chunk_video(case, cam, cs, ce, out_mp4, wh):
    """Write frames [cs,ce) of a camera (from its exact per-frame PNGs) to a temp chunk
    mp4. Long (1-min/~1800f) clips OOM SAM in one session, so we segment them in short
    contiguous pieces that each fit GPU memory and stitch the per-frame masks back."""
    vw = cv2.VideoWriter(out_mp4, cv2.VideoWriter_fourcc(*"mp4v"), 30,
                         (int(wh[0]), int(wh[1])))
    for f in range(cs, ce):
        img = cv2.imread(f"{case}/color/{cam}/{f}.png")
        if img is not None:
            vw.write(img)
    vw.release()


# Controller (hand) prompt strategy depends on the manipulation DIMENSIONALITY, which
# decides how much the hand gets occluded:
#
#   1d : rope/string -- thin, manipulated flat on a table. Hand stays clearly visible.
#   2d : cloth/sheet -- folding/flattening on a table. Hand stays clearly visible.
#   3d : lifting/gripping a volumetric object (e.g. a plush) out of plane. The hand is
#        heavily occluded and often reads as "arm" as it rises into frame.
#
# The forearm SUBTRACTION is essential in ALL of them: SAM3.1's "hand" concept does NOT
# stop at the wrist -- on a table the hand+forearm are one continuous skin region and
# "hand" alone segments the whole arm to the elbow. So every preset subtracts "human
# forearm" to get a wrist-down hand. The dimensionality differs only in the UNION:
#   1d/2d : hand detects reliably -> no arm-union needed.
#   3d    : hand often occluded/reads as arm -> also UNION "human arm" to not lose it.
#
# A residual ~4% of frames (flat-arm pose where SAM misses "human forearm") still leak
# the forearm -- a known limit of concept-based forearm detection.
#
# Object prompts stay explicit (object-specific); scene_type only drives the controller.
# controller_drop_area_spike: per camera, drop hand-mask frames whose area exceeds this
# x the camera's median hand area -- those are the residual forearm-leak frames (subtract
# missed). Other cameras cover those frames cleanly, so dropping the spike camera-frame
# removes the leak without losing the hand. Safe for 1d/2d (stable hand area on a table);
# OFF for 3d where occlusion makes the hand area swing a lot (a spike can be legit).
_VISIBLE_HAND = dict(controller_prompts=["hand"],
                     controller_subtract_prompts=["human forearm"],
                     controller_clean_min_area=300, controller_clean_close=2,
                     controller_clean_ratio=0.2, controller_drop_area_spike=2.0)
SCENE_PRESETS = {
    "1d": dict(_VISIBLE_HAND),                                   # rope
    "2d": dict(_VISIBLE_HAND),                                   # cloth
    "3d": dict(controller_prompts=["hand", "human arm"],         # lifted object
               controller_subtract_prompts=["human forearm"],
               controller_clean_min_area=500, controller_clean_close=2,
               controller_clean_ratio=0.2, controller_drop_area_spike=0.0),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base_path", default="./data/different_types")
    ap.add_argument("--case_name", required=True)
    ap.add_argument("--object_prompts", nargs="+", default=None,
                    help="object text prompt(s); required unless --groups controller. "
                         "The batch runners pass the episode JSON's prompt.")
    ap.add_argument("--groups", choices=["both", "object", "controller"], default="both",
                    help="which masks to (re)segment. 'controller' re-runs only the hand "
                         "(e.g. to switch scene_type) and preserves the existing object mask.")
    ap.add_argument("--scene_type", required=True, type=str.lower, choices=sorted(SCENE_PRESETS),
                    help="manipulation dimensionality -> controller(hand) prompt strategy: "
                         "'1d' rope, '2d' cloth (hand visible), '3d' lifting a volumetric "
                         "object (hand occluded -> arm-union). See SCENE_PRESETS.")
    # controller args default to None -> filled from the scene_type preset; pass any of
    # them explicitly to override the preset for that one field.
    ap.add_argument("--controller_prompts", nargs="+", default=None,
                    help="override scene preset: synonyms unioned into the controller.")
    ap.add_argument("--object_label", default=None, help="default: first object prompt")
    ap.add_argument("--controller_label", default="hand",
                    help="PhysTwin controller name (default 'hand')")
    ap.add_argument("--controller_subtract_prompts", nargs="*", default=None,
                    help="override scene preset: concepts subtracted from the controller "
                         "(e.g. 'human forearm') to keep it wrist-down.")
    ap.add_argument("--controller_clean_min_area", type=int, default=None,
                    help="override: drop controller components smaller than this (px)")
    ap.add_argument("--controller_clean_close", type=int, default=None,
                    help="override: morphological close radius (px) before component filtering")
    ap.add_argument("--controller_clean_ratio", type=float, default=None,
                    help="override: drop controller components smaller than this fraction "
                         "of the largest one (e.g. 0.2 removes detached fragments)")
    ap.add_argument("--controller_drop_area_spike", type=float, default=None,
                    help="override: per camera, drop hand frames with area > this x the "
                         "camera median (residual forearm-leak frames); 0 disables")
    ap.add_argument("--cameras", type=int, nargs="*", default=None)
    ap.add_argument("--prompt_frame", type=int, default=0)
    ap.add_argument("--chunk_frames", type=int, default=300,
                    help="process the video in contiguous forward chunks of this many "
                         "frames to bound GPU memory on long (1-min/~1800f) clips; clips "
                         "shorter than this are processed whole. 0 disables chunking.")
    ap.add_argument("--output_prob_thresh", type=float, default=0.5,
                    help="SAM mask confidence threshold for the OBJECT (lower = more "
                         "lenient, catches hard-to-see deformed shapes e.g. a rolled pad; "
                         "default 0.5). The object is also carried across chunks by point "
                         "prompt for continuity through deformation.")
    ap.add_argument("--grounding_batch_size", type=int, default=4, help="SAM3.1 detection/postprocess batch; 4 for 24GB RTX 4090")
    ap.add_argument("--version", default="sam3.1", choices=["sam3", "sam3.1"])
    ap.add_argument("--use_fa3", action="store_true", default=False,
                    help="Flash Attention 3 (Hopper only; OFF for Blackwell/RTX 5090)")
    args = ap.parse_args()

    # fill any controller field left unset from the scene_type preset (explicit args win)
    preset = SCENE_PRESETS[args.scene_type]
    for k, v in preset.items():
        if getattr(args, k) is None:
            setattr(args, k, v)
    print(f"[scene_type] '{args.scene_type}' -> controller_prompts={args.controller_prompts} "
          f"subtract={args.controller_subtract_prompts} "
          f"clean(min_area={args.controller_clean_min_area}, close={args.controller_clean_close}, "
          f"ratio={args.controller_clean_ratio})")

    # no default object prompt: a forgotten prompt must fail here, not silently segment
    # whatever a default happens to name
    if args.groups != "controller" and not args.object_prompts:
        ap.error("--object_prompts is required (unless --groups controller)")
    case = f"{args.base_path}/{args.case_name}"
    obj_label = args.object_label or (args.object_prompts[0] if args.object_prompts else None)
    if args.cameras is not None:
        cams = args.cameras
    else:
        cams = sorted(int(os.path.basename(p)) for p in glob.glob(f"{case}/depth/*")
                      if os.path.basename(p).isdigit())
    # group id -> (label, [prompts])
    want_ids = {"both": {0, 1}, "object": {0}, "controller": {1}}[args.groups]
    groups = [g for g in [(0, obj_label, args.object_prompts),
                          (1, args.controller_label, args.controller_prompts)]
              if g[0] in want_ids]
    print(f"[sam3.1] case={args.case_name} cameras={cams} groups={args.groups}")
    if 0 in want_ids:
        print(f"  obj0='{obj_label}' <- {args.object_prompts}")
    if 1 in want_ids:
        print(f"  obj1='{args.controller_label}' <- {args.controller_prompts} (union)")

    if args.grounding_batch_size < 1:
        ap.error("--grounding_batch_size must be positive")
    predictor = build_sam3_predictor(version=args.version, use_fa3=args.use_fa3)
    if args.version == "sam3.1":
        predictor.model.batched_grounding_batch_size = args.grounding_batch_size
        predictor.model.postprocess_batch_size = args.grounding_batch_size
        print(f"[memory] grounding/postprocess batch={args.grounding_batch_size}", flush=True)

    for cam in cams:
        mp4 = f"{case}/color/{cam}.mp4"
        if not os.path.isfile(mp4):
            print(f"  [skip] cam {cam}: {mp4} missing")
            continue
        _cap = cv2.VideoCapture(mp4)
        n_frames = int(_cap.get(cv2.CAP_PROP_FRAME_COUNT))
        WH = (int(_cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(_cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        _cap.release()
        cf = args.chunk_frames
        if cf and 0 < cf < n_frames:
            ranges = [(cs, min(cs + cf, n_frames)) for cs in range(0, n_frames, cf)]
            print(f"  [cam {cam}] {n_frames} frames -> {len(ranges)} memory-bounded chunks of <= {cf}")
        else:
            ranges = [(0, n_frames)]

        # 1) collect RAW group unions over the clip. A long (1-min) clip OOMs SAM in one
        #    session, so process contiguous SUB-VIDEO chunks (each fits GPU memory; the
        #    session is reset per chunk) and stitch per-frame masks by global frame index.
        raw = {oid: {} for oid, _, _ in groups}
        sub_raw = {}                       # controller subtract union (e.g. 'human forearm')
        thr = args.output_prob_thresh      # object mask confidence threshold (lower = lenient)
        chunk_path = f"{case}/.sam_chunk_{cam}.mp4"
        for ci, (cs, ce) in enumerate(ranges):
            src = mp4
            if len(ranges) > 1:
                write_chunk_video(case, cam, cs, ce, chunk_path, WH)
                src = chunk_path
            resp = predictor.handle_request(request=dict(
                type="start_session", resource_path=src, offload_video_to_cpu=True))
            sid = resp["session_id"]
            first = True
            for oid, label, prompts in groups:
                g, first = session_union(predictor, sid, prompts, first,
                                         thr if oid == 0 else 0.5)
                for lf, m in g.items():
                    raw[oid][cs + lf] = m if (cs + lf) not in raw[oid] else (raw[oid][cs + lf] | m)
                if oid == 1 and args.controller_subtract_prompts:
                    s, first = session_union(predictor, sid, args.controller_subtract_prompts, first)
                    for lf, m in s.items():
                        sub_raw[cs + lf] = m if (cs + lf) not in sub_raw else (sub_raw[cs + lf] | m)
            predictor.handle_request(request=dict(type="close_session", session_id=sid))
            # plain flush print (NOT tqdm: SAM3's logging setup makes tqdm's tty detection
            # fail so its bar never rendered). One line per chunk = the 300-frame steps.
            print(f"  [cam {cam}] SAM  {ce}/{n_frames} frames  (chunk {ci + 1}/{len(ranges)})",
                  flush=True)
        if len(ranges) > 1 and os.path.isfile(chunk_path):
            os.remove(chunk_path)

        # 2) post-process each group on the STITCHED result (subtract -> clean -> drop_spike)
        per_frame = defaultdict(dict)
        mask_info = {}
        for oid, label, prompts in groups:
            grp = raw[oid]
            if oid == 1 and args.controller_subtract_prompts:
                for f in list(grp.keys()):
                    if f in sub_raw:
                        grp[f] = grp[f] & ~sub_raw[f]
                print(f"  [cam {cam}] subtracted {args.controller_subtract_prompts} from controller")
            if oid == 1 and args.controller_clean_min_area > 0:
                for f in list(grp.keys()):
                    grp[f] = clean_mask(grp[f], args.controller_clean_min_area,
                                        args.controller_clean_close, args.controller_clean_ratio)
                print(f"  [cam {cam}] cleaned controller (close={args.controller_clean_close}, "
                      f"min_area={args.controller_clean_min_area})")
            if oid == 1 and args.controller_drop_area_spike and args.controller_drop_area_spike > 0:
                areas = {f: int(m.sum()) for f, m in grp.items() if m.any()}
                if len(areas) >= 5:
                    med = float(np.median(list(areas.values())))
                    drop = [f for f, a in areas.items()
                            if a > args.controller_drop_area_spike * med]
                    for f in drop:
                        grp[f] = np.zeros_like(grp[f])
                    print(f"  [cam {cam}] dropped {len(drop)} area-spike hand frames "
                          f"(>{args.controller_drop_area_spike}x median={med:.0f}px; forearm leak)")
            ncov = sum(1 for m in grp.values() if m.any())
            if ncov > 0:
                mask_info[oid] = label
                for f, m in grp.items():
                    if m.any():
                        per_frame[f][oid] = m.astype(np.uint8)
            print(f"  [cam {cam}] obj{oid} '{label}': {ncov} frames covered")

        os.makedirs(f"{case}/mask/{cam}", exist_ok=True)
        # clear stale frames ONLY for the groups we re-ran (so a controller-only re-run
        # preserves the object mask), and so sparser coverage leaves no old frames behind
        for oid in want_ids:
            d = f"{case}/mask/{cam}/{oid}"
            if os.path.isdir(d):
                shutil.rmtree(d)
        # merge mask_info: keep entries for groups we did NOT re-run
        info_path = f"{case}/mask/mask_info_{cam}.json"
        merged = json.load(open(info_path)) if os.path.isfile(info_path) else {}
        for oid in want_ids:
            merged.pop(str(oid), None)
        merged.update({str(k): v for k, v in mask_info.items()})
        with open(info_path, "w") as f:
            json.dump(merged, f)
        for fr, objs in per_frame.items():
            for oid, m in objs.items():
                os.makedirs(f"{case}/mask/{cam}/{oid}", exist_ok=True)
                Image.fromarray((m * 255).astype(np.uint8)).save(f"{case}/mask/{cam}/{oid}/{fr}.png")
        print(f"  [cam {cam}] saved obj_ids {sorted(mask_info)} over {len(per_frame)} frames")

    print(f"[done] masks under {case}/mask/")


if __name__ == "__main__":
    main()
