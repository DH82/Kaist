import os
import os.path as osp
import numpy as np
import open3d as o3d
import json
import copy
import pickle
import cv2
import torch
import trimesh
from tqdm import tqdm
from argparse import ArgumentParser
from scipy.optimize import minimize
from sklearn.cluster import KMeans
from models.mano_wrapper import MANO

from pytorch3d.structures import Meshes
from pytorch3d.renderer import (
    PerspectiveCameras,
    RasterizationSettings,
    MeshRenderer,
    MeshRasterizer,
    SoftPhongShader,
    BlendParams,
    PointLights,
)
from pytorch3d.renderer.mesh import TexturesVertex
import random
import hashlib
from pathlib import Path
from pytorch3d.transforms import matrix_to_rotation_6d, rotation_6d_to_matrix
ALGORITHM_VERSION = "mano-geometric-shared-shape-v6"
from timer_logger import TimerLogger

def set_all_seeds(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # if you are using multi-GPU.
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

parser = ArgumentParser()
parser.add_argument("--hamba_base_path", type=str, default='/workspace/deform_hoi/Hamba/hand_recon_out')
parser.add_argument("--phystwin_base_path", type=str, default='/workspace/deform_hoi/PhysTwin1/data/different_types')
parser.add_argument("--case_name", type=str, default='double_stretch_zebra')
parser.add_argument("--temp_weight", type=float, default=5e+5)
parser.add_argument("--hamba_weight", type=float, default=0.5)
parser.add_argument("--start_frame", type=int, default=0)
parser.add_argument("--end_frame", type=int, default=-1,
                    help="exclusive upper bound on the frame loop; -1 = frame_num (full sequence)")
parser.add_argument("--w_pen", type=float, default=0.0,
                    help="weight for the 3D hand->object non-penetration loss (0 = disabled)")
parser.add_argument("--init_from_log", type=str, default=None,
                    help="chain the temporal init from an existing fit: load "
                         "<case>/<init_from_log>/hand_reg_params.pkl frame start_frame-1 "
                         "as prev_params instead of a fresh HaMBA init")
parser.add_argument("--w_mask", type=float, default=0.0,
                    help="weight of the hand-seg-mask silhouette alignment loss (0 = off). "
                         "One-sided: penalizes squared distance-transform px of projected verts "
                         "falling OUTSIDE the per-cam hand mask.")
parser.add_argument("--pen_band_mm", type=float, default=20.0,
                    help="only object points within this distance (mm) of the hand count toward penetration")
parser.add_argument("--w_mask_cover", type=float, default=0.0,
                    help="weight of the two-sided mask COVERAGE loss (0 = off): mean squared px "
                         "distance from sampled inside-mask pixels to the nearest projected vert. "
                         "Complements --w_mask (one-sided, verts must stay inside the mask).")
parser.add_argument("--cover_max_px", type=float, default=40.0,
                    help="mask pixels farther than this (px) from every projected vert are ignored "
                         "by the coverage loss (robustness against forearm/glove-cuff mask area "
                         "that MANO cannot cover)")
parser.add_argument("--ball_anchor_npy", type=str, default=None,
                    help="ball_centers.npy path: during annotated grip frames the temporal-verts "
                         "target is shifted by the ball displacement (prev_verts + Δball), so the "
                         "strong temp prior enforces 'move WITH the ball' instead of 'stay put'. "
                         "The triangulated ball is far more reliable than blurred hand masks/HaMBA "
                         "at fast throw frames; a rigid hold implies constant hand-ball offset.")
parser.add_argument("--ball_anchor_frames_right", type=str, default="",
                    help="grip windows of the RIGHT hand, e.g. '0-7,34-43' (inclusive)")
parser.add_argument("--ball_anchor_frames_left", type=str, default="",
                    help="grip windows of the LEFT hand, e.g. '17-25,53-62' (inclusive)")
parser.add_argument("--num_iters", type=int, default=1500)
parser.add_argument("--log", type=str, default='v1')
parser.add_argument("--seed", type=int, default=42)
parser.add_argument("--num_hands", type=int, default=-1,
                    help="number of hands to fit; -1 = auto (1 if 'single' in case name else 2)")
parser.add_argument("--num_cams", type=int, default=3,
                    help="number of cameras used for the 2D losses (MediaPipe/HaMBA/mask). "
                         "Cams without a HaMBA results.pt are tolerated (HaMBA GT skipped for them).")

parser.add_argument("--resume", action="store_true", help="Resume an atomic per-frame checkpoint with matching fit arguments")
parser.add_argument("--observations-cache", default=None, help="Frozen original MediaPipe output; identity checked against source RGB and detector implementation")
parser.add_argument("--stop-after-frames", type=int, default=0, help="Testing: stop after this many newly completed frames; checkpoint remains resumable")
parser.add_argument('--wrist_weight', type=float, default=1.0,
                    help='Mask weight proximal to observed wrist; 1 preserves all pixels')
parser.add_argument('--w_silhouette', type=float, default=0.0,
                    help='Soft rendered mask + boundary EDT loss weight')
parser.add_argument('--silhouette_size', type=int, default=96)
parser.add_argument('--silhouette_every', type=int, default=5)
parser.add_argument('--staged_fit', action='store_true', help='Global, proximal, then full pose fitting')
parser.add_argument('--pose_temp_weight', type=float, default=None,
                    help='Separate finger-pose temporal weight; default preserves temp_weight')
parser.add_argument('--w_pose_prior', type=float, default=0.0,
                    help='Rotation prior to initial HaMBA finger pose')
parser.add_argument('--shared_shape', action='store_true', help='Calibrate one shared beta per hand before tracking')
parser.add_argument('--shared_shape_file', default=None, help='Reuse a validated shared-shape calibration')
parser.add_argument('--shape_only', action='store_true', help='Calibrate shared shape and skip temporal tracking')
parser.add_argument('--shape_frames', default='', help='Comma-separated representative frame IDs; empty selects by confidence')
parser.add_argument('--shape_frame_count', type=int, default=6)
parser.add_argument('--shape_iters', type=int, default=3000)
parser.add_argument('--shape_lr', type=float, default=1e-4)
parser.add_argument('--shape_prior_weight', type=float, default=1.0)
parser.add_argument('--shape_max_delta', type=float, default=1.0)
parser.add_argument('--camera_init_iters', type=int, default=300, help='Rigid multi-view HaMBA reprojection initialization; 0 disables')
parser.add_argument('--fit_shape', action='store_true', help='Refine betas with pose and translation in each frame')
parser.add_argument('--fit_shape_lr', type=float, default=2e-3)
parser.add_argument('--fit_shape_max_delta', type=float, default=0.0, help='Per-frame beta change bound; 0 disables')
parser.add_argument('--fit_shape_temporal_weight', type=float, default=1.0, help='Penalty on beta changes from previous frame')
parser.add_argument('--robust_joint_delta',type=float,default=0.0,help='Robust 2D joint loss transition in pixels; 0 preserves MSE')
args = parser.parse_args()
if args.robust_joint_delta < 0: parser.error('robust_joint_delta must be nonnegative')
if args.fit_shape_lr <= 0 or args.fit_shape_max_delta < 0 or args.fit_shape_temporal_weight < 0:
    parser.error('Invalid per-frame shape optimization settings')
if args.camera_init_iters < 0: parser.error('camera_init_iters must be nonnegative')
if not 0 <= args.wrist_weight <= 1: parser.error('wrist_weight must be in [0,1]')
if args.silhouette_size < 32 or args.silhouette_every < 1: parser.error('invalid silhouette sampling')
if args.shape_only and not (args.shared_shape or args.shared_shape_file): parser.error('shape_only requires shared shape')
if args.shape_frame_count < 1 or args.shape_iters < 1 or args.shape_lr <= 0 or args.shape_max_delta <= 0 or args.shape_prior_weight < 0: parser.error('invalid shape calibration settings')

set_all_seeds(args.seed)

hamba_base_path = args.hamba_base_path
phystwin_base_path = args.phystwin_base_path
case_name = args.case_name
log = args.log
temp_weight = args.temp_weight
hamba_weight = args.hamba_weight
start_frame = args.start_frame
end_frame = args.end_frame
w_pen = args.w_pen
w_mask = args.w_mask
w_mask_cover = args.w_mask_cover
cover_max_px = args.cover_max_px
pen_band_mm = args.pen_band_mm
num_iters = args.num_iters

num_hands = args.num_hands if args.num_hands > 0 else (1 if 'single' in case_name else 2)
NUM_CAMS = args.num_cams

# --- ball-anchor: per-side grip-frame sets + ball centers (world) ---
def _parse_frame_windows(s):
    out = set()
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        a, b = (tok.split("-") + [tok])[:2] if "-" in tok else (tok, tok)
        out.update(range(int(a), int(b) + 1))
    return out

BALL_C = np.load(args.ball_anchor_npy) if args.ball_anchor_npy else None
BALL_FRAMES = {True: _parse_frame_windows(args.ball_anchor_frames_right),
               False: _parse_frame_windows(args.ball_anchor_frames_left)}

def ball_temp_shift(fidx, is_right):
    """Δball world shift for the temporal-verts target, or None outside grip windows."""
    if BALL_C is None or fidx <= 0:
        return None
    win = BALL_FRAMES[bool(is_right)]
    if (fidx in win) and (fidx - 1 in win) and fidx < len(BALL_C):
        return BALL_C[fidx] - BALL_C[fidx - 1]
    return None

RES = [848., 480.]
cx, cy = RES[0] / 2., RES[1] / 2.
fx = fy = 427. #16562.5000 # always a constant in hamba

# real per-cam 3x3 intrinsics (from the case's metadata.json); set in process_hands.
# Used for all projections of OUR predicted hand into the real cameras. The HaMBA 2D
# GT (compute_hamba_2d_gt) keeps HaMBA's own convention since that defines the target
# pixels. Falls back to the hardcoded fx/fy/cx/cy above when metadata is missing.
REAL_KS = None

# per-cam hand seg-mask id (mask ids are NOT consistent across cams, e.g. cam1 of
# single_clift_lift_cloth_1 has hand/cloth swapped); set in process_hands from
# mask/mask_info_{c}.json when --w_mask > 0.
HAND_MASK_IDS = None
SHARED_BETAS = {}

root_idx = [215, 206]

with open('data/mano_faces.pkl', 'rb') as f:
    FACES = pickle.load(f)

mano_cfg = {'data_dir': 'data_process/Hamba/_DATA/data/', 'model_path': 'data_process/Hamba/_DATA/data/mano', 'gender': 'neutral', 'num_hand_joints': 15, 'mean_params': 'data_process/Hamba/_DATA/data/mano_mean_params.npz', 'create_body_pose': False}
mano = MANO(**mano_cfg).to('cuda')

device = 'cuda' if torch.cuda.is_available() else 'cpu'

invalid_mp = []

filter_list = ['double_stretch_zebra', 'single_lift_cloth_1', 'single_lift_cloth_3', 'single_push_rope_4']

def write_pcd(pts, name):
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(pts)  # XYZ
    o3d.io.write_point_cloud(f"../debug/{name}.ply", pcd)


def hamba_3d_to_2d(points_cam, focal=None, cam_idx=None):

    # Project points from camera coordinates to image plane
    x = points_cam[:, 0]
    y = points_cam[:, 1]
    z = points_cam[:, 2]

    if cam_idx is not None and REAL_KS is not None:
        K = REAL_KS[cam_idx]
        fx_, fy_, cx_, cy_ = K[0][0], K[1][1], K[0][2], K[1][2]
    elif focal is None:
        fx_, fy_ = fx, fy
        cx_, cy_ = cx, cy
    else:
        fx_ = fy_ = focal
        cx_, cy_ = cx, cy

    u = fx_ * x / z + cx_
    v = fy_ * y / z + cy_

    # Stack to get 2D pixel coordinates
    if isinstance(points_cam, np.ndarray):
        points_2d = np.stack([u, v], axis=-1)
    else:
        points_2d = torch.stack([u, v], dim=-1)

    return points_2d


def compute_hamba_2d_gt(frame_data, is_right):

    res = []
    valid = []

    for cidx in range(NUM_CAMS):

        if frame_data[cidx] is None:
            valid.append(False)
            continue

        vertices = frame_data[cidx]['pred_vertices'][0].detach().cpu().numpy()
        camera_translation = frame_data[cidx]['pred_cam_t_hamba']

        # preprocess vertices - fetch reference camera mesh and project it to world coord space
        if not is_right:
            vertices[:, 0] *= -1. 

        vertices += camera_translation
        points_2d = hamba_3d_to_2d(vertices, focal=16562.5000 )

        valid.append(True)
        res.append(points_2d)

    return np.asarray(res), valid


def file_digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''): h.update(block)
    return h.hexdigest()

def rng_state():
    return dict(python=random.getstate(), numpy=np.random.get_state(), torch=torch.get_rng_state(), cuda=torch.cuda.get_rng_state_all())

def restore_rng(state):
    random.setstate(state['python']); np.random.set_state(state['numpy'])
    torch.set_rng_state(state['torch'].cpu()); torch.cuda.set_rng_state_all([x.cpu() for x in state['cuda']])

def frozen_observations(frame_num):
    import inspect
    cache = Path(args.observations_cache or f"{phystwin_base_path}/{case_name}/{log}/observations.pkl")
    files = sorted(Path(f"{phystwin_base_path}/{case_name}/color").glob('*/*.png'))
    import mediapipe
    identity = dict(frames=frame_num, cams=NUM_CAMS, hands=num_hands,
        detector=hashlib.sha256(inspect.getsource(compute_mediapipe_2d_gt).encode()).hexdigest(),
        mediapipe_version=mediapipe.__version__, seed=args.seed,
        images={str(x.relative_to(Path(phystwin_base_path)/case_name)):file_digest(x) for x in files})
    if cache.exists():
        with cache.open('rb') as f: stored=pickle.load(f)
        if stored.get('identity') != identity: raise RuntimeError('Frozen detector input/code mismatch; use a new attempt/cache')
        restore_rng(stored['rng_after_detection'])
        return stored['observations']
    output=compute_mediapipe_2d_gt(frame_num)
    cache.parent.mkdir(parents=True,exist_ok=True)
    tmp=cache.with_suffix('.tmp')
    with tmp.open('wb') as f: pickle.dump(dict(identity=identity, observations=output, rng_after_detection=rng_state()),f)
    os.replace(tmp,cache)
    return output

def compute_mediapipe_2d_gt(frame_num):
    import mediapipe as mp

    # Initialize MediaPipe Hands
    mp_hands = mp.solutions.hands
    mp_drawing = mp.solutions.drawing_utils

    out_dict = {}

    for cidx in range(NUM_CAMS):
        out_dict[cidx] = {'kpts': {'Left': [], 'Right': []}, 'conf': {'Left': [], 'Right': []}}

        hand_det = mp_hands.Hands(static_image_mode=False, max_num_hands=num_hands, min_detection_confidence=0.5, min_tracking_confidence=0.5) 
        import imageio.v2 as iio
        _vd = os.path.join(phystwin_base_path, case_name, 'mediapipe')
        os.makedirs(_vd, exist_ok=True)
        vid = iio.get_writer(os.path.join(_vd, f'{cidx}.mp4'), fps=30,
                             codec='libx264', macro_block_size=None)

        for fidx in tqdm(range(frame_num)):

            # Load the image
            image_path = os.path.join(phystwin_base_path, case_name, 'color', f'{cidx}', f'{fidx}.png')
            image = cv2.imread(image_path)
            image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

            # Process the image and detect hands
            results = hand_det.process(image_rgb)

            if results.multi_hand_landmarks:
                for hand_landmarks in results.multi_hand_landmarks:
                    # Draw landmarks on the image
                    mp_drawing.draw_landmarks(image, hand_landmarks, mp_hands.HAND_CONNECTIONS)

                # Show the output
                out_path = f'../debug/mediapipe/{case_name}/{cidx}/{fidx}.png'
                os.makedirs(os.path.dirname(out_path), exist_ok=True)

                cv2.imwrite(out_path, image)
            else:
                print("No hands detected.")

                for side in ['Left', 'Right']:
                    out_dict[cidx]['kpts'][side].append(np.zeros((21, 3))) 
                    out_dict[cidx]['conf'][side].append(np.zeros((21,)))

                #out_dict[cidx]['label'].append('')

                continue

            if results.multi_hand_landmarks and results.multi_handedness:

                num_hands_detected = len(results.multi_hand_landmarks)

                if num_hands == 2: 
                    if num_hands_detected == 1:
                        det_label = results.multi_handedness[0].classification[0].label

                        undet_label = 'Left' if det_label == 'Right' else 'Right'

                        out_dict[cidx]['kpts'][undet_label].append(np.zeros((21, 3))) 
                        out_dict[cidx]['conf'][undet_label].append(np.zeros((21,)))

                    elif num_hands_detected == 2:
                        label_1 = results.multi_handedness[0].classification[0].label
                        label_2 = results.multi_handedness[1].classification[0].label

                        if label_1 == label_2:
                            for side in ['Left', 'Right']:
                                out_dict[cidx]['kpts'][side].append(np.zeros((21, 3))) 
                                out_dict[cidx]['conf'][side].append(np.zeros((21,)))

                            continue

                for hand_idx, hand_landmarks in enumerate(results.multi_hand_landmarks):

                    label = results.multi_handedness[hand_idx].classification[0].label

                    if num_hands == 1:
                        if label == 'Right':
                            print("No hands detected.")

                            for side in ['Left', 'Right']:
                                out_dict[cidx]['kpts'][side].append(np.zeros((21, 3))) 
                                out_dict[cidx]['conf'][side].append(np.zeros((21,)))
                                #out_dict[cidx]['label'].append('')

                            continue
                        
                        else:
                            side = 'Right'
                            out_dict[cidx]['kpts'][side].append(np.zeros((21, 3))) 
                            out_dict[cidx]['conf'][side].append(np.zeros((21,)))

                    keypoints = []
                    confidences = []

                    for invalid in invalid_mp:
                        if case_name == invalid[0] and cidx == invalid[1] and label == invalid[2]:
                            out_dict[cidx]['kpts'][label].append(np.zeros((21, 3)))
                            out_dict[cidx]['conf'][label].append(np.zeros((21,)))
                            print('invalid')
                            continue

                    for lm in hand_landmarks.landmark:
                        x, y, z = lm.x, lm.y, lm.z
                        keypoints.append([x, y, z])
                        # MediaPipe doesn't give per-joint confidence — use overall hand score
                        score = results.multi_handedness[hand_idx].classification[0].score
                        confidences.append(score)

                    out_dict[cidx]['kpts'][label].append(np.array(keypoints))
                    out_dict[cidx]['conf'][label].append(np.array(confidences))
                    #out_dict[cidx]['label'][label].append(label)

            else:
                print("No hands detected.")

            # per-cam prediction video ({case}/mediapipe/{cam}.mp4): drawn landmarks
            # when detected, raw frame otherwise (continuous stream)
            vid.append_data(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))

        vid.close()
        for side in ['Left', 'Right']:
            print(cidx, len(out_dict[cidx]['kpts'][side]))
            print(cidx, len(out_dict[cidx]['conf'][side]))

            out_dict[cidx]['kpts'][side] = np.concatenate([out_dict[cidx]['kpts'][side][i][None] for i in range(frame_num)], 0)
            out_dict[cidx]['kpts'][side] = out_dict[cidx]['kpts'][side][:, :, :2]*RES

            out_dict[cidx]['conf'][side] = np.concatenate([out_dict[cidx]['conf'][side][i][None] for i in range(frame_num)], 0)
            out_dict[cidx]['conf'][side][out_dict[cidx]['conf'][side]<0.9] = 0.
            out_dict[cidx]['conf'][side] = out_dict[cidx]['conf'][side][..., None]

    return out_dict


def render_hamba_videos(hand_data, frame_num):
    """Per-cam mp4 of the raw HaMBA predictions: projected MANO vertices scattered on
    the color frames ({case}/hamba/{cam}.mp4). Green = right hand, red = left."""
    import imageio.v2 as iio
    vd = os.path.join(phystwin_base_path, case_name, 'hamba')
    os.makedirs(vd, exist_ok=True)
    for cidx in range(NUM_CAMS):
        if hand_data[cidx] is None:
            continue
        w = iio.get_writer(os.path.join(vd, f'{cidx}.mp4'), fps=30,
                           codec='libx264', macro_block_size=None)
        for fidx in range(frame_num):
            image_path = os.path.join(phystwin_base_path, case_name, 'color',
                                      f'{cidx}', f'{fidx}.png')
            img = cv2.imread(image_path)
            if img is None:
                break
            for hidx in range(num_hands) if num_hands == 2 else [1]:
                key = f'{fidx}_{hidx}'
                if key not in hand_data[cidx]:
                    continue
                fd = hand_data[cidx][key]
                if fd is None:
                    continue
                is_right = 0 if (hidx == 0 and num_hands == 2) else 1
                v = fd['pred_vertices'][0].detach().cpu().numpy().copy()
                if not is_right:
                    v[:, 0] *= -1.
                v = v + fd['pred_cam_t_hamba']
                pts = hamba_3d_to_2d(v, focal=16562.5000)
                col = (0, 255, 0) if is_right else (0, 0, 255)
                for x, y in pts[::6]:
                    xi, yi = int(round(x)), int(round(y))
                    if 0 <= xi < img.shape[1] and 0 <= yi < img.shape[0]:
                        cv2.circle(img, (xi, yi), 1, col, -1)
            w.append_data(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        w.close()
        print(f'hamba video: {vd}/{cidx}.mp4')


def render(vertices, is_right, cam_idx=None):
    # Load your mesh (vertices in world coords)
    verts = torch.tensor(vertices, dtype=torch.float32).clone().unsqueeze(0)  # (1, V, 3)

    if is_right:
        faces = torch.tensor(FACES['left'], dtype=torch.int64).unsqueeze(0)      # (1, F, 3)
    else:
        faces = torch.tensor(FACES['right'], dtype=torch.int64).unsqueeze(0)      # (1, F, 3)
    verts = verts.to(device)
    # OpenCV (+x right,+y down) -> PyTorch3D (+x left,+y up).
    verts[..., :2] *= -1
    faces = faces.to(device)

    blue_color = torch.tensor([0.8, 0.5, 0.8], device=device)
    textures = TexturesVertex(verts_features=blue_color.expand_as(verts))

    # Make mesh
    mesh = Meshes(verts=verts, faces=faces, textures=textures)

    image_size = (int(RES[1]), int(RES[0]))

    if cam_idx is not None and REAL_KS is not None:
        K = REAL_KS[cam_idx]
        fx_, fy_, cx_, cy_ = K[0][0], K[1][1], K[0][2], K[1][2]
    else:
        fx_, fy_, cx_, cy_ = fx, fy, cx, cy

    # Camera setup
    cameras = PerspectiveCameras(
        focal_length=((fx_, fy_),),
        principal_point=((cx_, cy_),),
        image_size=(image_size,),
        device=device,
        in_ndc=False
    )

    # Renderer settings
    raster_settings = RasterizationSettings(
        image_size=image_size,
        blur_radius=0.0,
        faces_per_pixel=1,
        bin_size=None
    )

    
    lights = PointLights(
            device=device,
            location=[[-2.0, -2.0, 2.0]],  # Light position
            #ambient_color=((0.1, 0.1, 0.1),),   # Ambient strength (soft fill light)
            #diffuse_color=((1.0, 1.0, 1.0),),   # Diffuse strength (main surface lighting)
            #specular_color=((0.2, 0.2, 0.2),)   # Specular strength (shininess/bright spots)
        )
    renderer = MeshRenderer(
        rasterizer=MeshRasterizer(cameras=cameras, raster_settings=raster_settings),
        shader=SoftPhongShader(device=device, cameras=cameras, lights=lights)
    )

    '''
    # debug
    fragments = renderer.rasterizer(mesh)
    depth_pred = fragments.zbuf.squeeze()  # (H, W)
    depth_pred = torch.flip(depth_pred, dims=(0, 1))

    depth = depth_pred.clone().detach().cpu().numpy()
    depth[depth < 1e-3] = np.nan

    depth_min = np.nanmin(depth)
    depth_max = np.nanmax(depth)
    depth_norm = (255 * (depth - depth_min) / (depth_max - depth_min)).astype(np.uint8)

    depth_colored = cv2.applyColorMap(depth_norm, cv2.COLORMAP_PLASMA)

    cv2.imwrite("../debug/depth_pred.png", depth_colored)
    '''
    images = renderer(mesh)  # (1, H, W, 3), values in [0, 1]

    # Convert to CPU numpy image
    rgba = images.cpu().numpy()[0]
    #rgba = np.flip(rgba, axis=(0))

    rgb_render = rgba[..., :3]  # (H, W, 3)
    alpha = rgba[..., 3:4]  # (H, W, 1)

    #alpha = np.transpose(alpha, (1, 0, 2)) 
    alpha = np.repeat(alpha, 3, axis=-1) 
    alpha[alpha > 0.3] = 1.

    #rgb_render = np.transpose(rgb_render, (1, 0, 2))

    return rgb_render, alpha


def translation_error(translation, verts_wld, T_w2c, gt_2d, gt_valid):

    verts_wld_translated = verts_wld + translation
    vertices_world_hom = np.hstack([verts_wld_translated, np.ones((verts_wld_translated.shape[0], 1))])  # (N, 4)

    pred_2d = []

    # need to change it to batch operation
    for cam_idx in range(NUM_CAMS):
        # Apply inverse transform
        vertices_hom = (T_w2c[cam_idx] @ vertices_world_hom.T).T  # (N, 4)

        # Optionally extract 3D camera coordinates
        vertices_cam = vertices_hom[:, :3]

        pred_2d.append(hamba_3d_to_2d(vertices_cam, cam_idx=cam_idx))

    pred_2d = np.asarray(pred_2d)

    return np.mean((pred_2d[gt_valid] - gt_2d) ** 2)


def optimize_transl(verts, frame_data, c2ws, is_right):
    
    T_c2w = c2ws
    T_w2c = [np.linalg.inv(x) for x in T_c2w]

    gt_2d, gt_valid = compute_hamba_2d_gt(frame_data, is_right)

    translation = minimize(
                translation_error,
                x0=np.zeros((3)),
                args=(verts, T_w2c, gt_2d, gt_valid),
                method='Powell'
            ).x

    return translation


def penetration_loss(verts_wld, faces_t, obj_pts, band_m):
    """Differentiable non-penetration loss: pushes the hand mesh off object points
    that lie inside its closed volume.
    verts_wld (V,3), faces_t (F,3) long, obj_pts (M,3) all on device; band_m in metres.
    Inside/outside is approximated by the signed distance to the nearest hand vertex
    along that vertex's outward normal (negative => inside => penetrating). Returns a
    scalar = mean penetration depth in mm over near object points (grad flows to verts)."""
    mesh = Meshes(verts=[verts_wld], faces=[faces_t])
    vn = mesh.verts_normals_packed()                    # (V,3) outward (right-hand winding)
    d = torch.cdist(obj_pts, verts_wld)                 # (M,V)
    dmin, idx = d.min(dim=1)                            # (M,)
    within = dmin < band_m
    if int(within.sum()) == 0:
        return verts_wld.sum() * 0.0
    p = obj_pts[within]
    vi = verts_wld[idx[within]]
    ni = vn[idx[within]]
    signed = ((p - vi) * ni).sum(-1)                    # >0 outside, <0 inside
    pen_mm = torch.relu(-signed) * 1000.0               # penetration depth in mm
    return pen_mm.mean()


def validate_hand_masks(frame_num):
    """Fail on broken inputs; valid empty masks are allowed for occluded hands."""
    global HAND_MASK_IDS
    HAND_MASK_IDS = {}
    base = Path(phystwin_base_path) / case_name / 'mask'
    first = max(0, start_frame)
    last = frame_num if end_frame < 0 else min(end_frame, frame_num)
    for cidx in range(NUM_CAMS):
        info_path = base / f'mask_info_{cidx}.json'
        info = json.loads(info_path.read_text())
        ids = [str(k) for k, v in info.items() if isinstance(v, str) and 'hand' in v.lower()]
        if len(ids) != num_hands:
            raise ValueError(f'{info_path}: expected {num_hands} hand IDs, got {ids}')
        HAND_MASK_IDS[cidx] = ids[0]
        # Two-hand ID assignment scans earlier frames, so validate those too.
        for mid in ids:
            for fidx in range(0 if num_hands == 2 else first, last):
                path = base / str(cidx) / mid / f'{fidx}.png'
                image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                if image is None:
                    raise ValueError(f'Missing/unreadable SAM3 hand mask: {path}')
                if image.shape != (int(RES[1]), int(RES[0])):
                    raise ValueError(f'SAM3 mask/RGB size mismatch: {path}: {image.shape}')
                if not np.isin(image, [0, 255]).all():
                    raise ValueError(f'SAM3 mask must be binary 0/255: {path}')
    print(f'mask losses ON (w_mask={w_mask}, w_mask_cover={w_mask_cover}), per-cam hand mask ids: {HAND_MASK_IDS}')


def mask_dt_maps(fidx, ids=None):
    """Per-cam distance-transform maps of the hand seg mask for frame fidx.
    Returns list of 3 tensors (1,1,H,W) in px (0 inside the mask) or None per cam."""
    dts = []
    for cidx in range(NUM_CAMS):
        m_path = f"{phystwin_base_path}/{case_name}/mask/{cidx}/{(ids or HAND_MASK_IDS)[cidx]}/{fidx}.png"
        m = cv2.imread(m_path, cv2.IMREAD_GRAYSCALE)
        if m is None or (m > 127).sum() < 50:
            dts.append(None)
            continue
        outside = (m <= 127).astype(np.uint8)
        dt = cv2.distanceTransform(outside, cv2.DIST_L2, 3)
        dts.append(torch.tensor(dt, dtype=torch.float32, device=device)[None, None])
    return dts


def mask_cover_pts(fidx, max_pts=1500, ids=None):
    """Per-cam subsampled inside-mask pixel coords (M,2) px for the coverage loss,
    or None per cam when the mask is missing/tiny."""
    pts = []
    for cidx in range(NUM_CAMS):
        m_path = f"{phystwin_base_path}/{case_name}/mask/{cidx}/{(ids or HAND_MASK_IDS)[cidx]}/{fidx}.png"
        m = cv2.imread(m_path, cv2.IMREAD_GRAYSCALE)
        if m is None or (m > 127).sum() < 50:
            pts.append(None)
            continue
        ys, xs = np.nonzero(m > 127)
        step = max(1, len(ys) // max_pts)
        uv = np.stack([xs[::step], ys[::step]], 1).astype(np.float32)
        pts.append(torch.tensor(uv, device=device))
    return pts


def wrist_weights(uv, wrist):
    """Fixed observation-derived region; never move the evaluation cut with the fit."""
    if wrist is None:
        return torch.ones(uv.shape[:-1], device=uv.device)
    origin, axis = wrist
    t = ((uv - origin) * axis).sum(-1) / axis.square().sum().clamp_min(1.0)
    return args.wrist_weight + (1.0 - args.wrist_weight) * torch.sigmoid((t - 0.25) / 0.10)


def silhouette_targets(fidx, ids, mp_out, is_right):
    side = 'Left' if is_right else 'Right'
    targets = []
    for cidx in range(NUM_CAMS):
        k = np.asarray(mp_out[cidx]['kpts'][side][fidx])
        confidence = np.asarray(mp_out[cidx]['conf'][side][fidx])
        wrist = None
        axis = k[[5, 9, 13, 17]].mean(0) - k[0]
        if np.isfinite(k).all() and float(confidence.mean()) >= 0.5 and np.linalg.norm(axis) >= 8:
            wrist = (torch.as_tensor(k[0], dtype=torch.float32, device=device),
                     torch.as_tensor(axis, dtype=torch.float32, device=device))
        image = cv2.imread(f'{phystwin_base_path}/{case_name}/mask/{cidx}/{ids[cidx]}/{fidx}.png', 0)
        m = image > 127
        if m.sum() < 50:
            targets.append(dict(wrist=wrist, valid=False))
            continue
        ys, xs = np.nonzero(m)
        center = np.array([(xs.max()+xs.min())/2, (ys.max()+ys.min())/2], np.float32)
        span = float(max(xs.max()-xs.min(), ys.max()-ys.min()) + 80)
        size = args.silhouette_size
        # Fixed crop based solely on observations; identical for every candidate.
        transform = np.array([[size/span, 0, size/2-center[0]*size/span],
                              [0, size/span, size/2-center[1]*size/span]], np.float32)
        cropped = cv2.warpAffine(m.astype(np.uint8), transform, (size,size), flags=cv2.INTER_NEAREST)
        boundary = m ^ cv2.erode(m.astype(np.uint8), np.ones((3,3),np.uint8)).astype(bool)
        dt = cv2.distanceTransform((~boundary).astype(np.uint8), cv2.DIST_L2, 5)
        dt = cv2.warpAffine(dt, transform, (size,size), flags=cv2.INTER_LINEAR, borderValue=80)
        yy, xx = torch.meshgrid(torch.arange(size,device=device),torch.arange(size,device=device),indexing='ij')
        uv = torch.stack([xx,yy],-1).float()*span/size + torch.as_tensor(center-span/2,device=device)
        targets.append(dict(wrist=wrist,valid=True,
            center=torch.as_tensor(center,device=device),span=span,
            mask=torch.as_tensor(cropped,dtype=torch.float32,device=device),
            boundary_dt=torch.as_tensor(dt,dtype=torch.float32,device=device),
            weights=wrist_weights(uv,wrist)))
    return targets


def rendered_silhouette_loss(pred_2d_v, pred_z, faces, targets):
    """Differentiable triangle rasterization, occupancy and boundary-distance terms.
    Adapted from the idea in FreiHAND; not a reproduction of its annotation pipeline.
    """
    from pytorch3d.renderer.mesh.rasterize_meshes import rasterize_meshes
    size = args.silhouette_size
    verts = []
    for c,t in enumerate(targets):
        center = t.get('center', pred_2d_v[c].detach().mean(0))
        span = t.get('span', max(RES))
        xy = -(pred_2d_v[c] - center) * (2.0/span)
        verts.append(torch.cat([xy,pred_z[c][:,None]],-1))
    meshes = Meshes(verts=verts,faces=[faces]*NUM_CAMS)
    sigma = (2.0/size)**2 * 0.5
    pix,_,_,dists = rasterize_meshes(meshes,image_size=size,blur_radius=4*sigma,
                                    faces_per_pixel=4,perspective_correct=False)
    probability = torch.sigmoid(-dists/sigma) * (pix >= 0)
    alpha = 1.0 - (1.0-probability).prod(-1)
    terms=[]
    for c,t in enumerate(targets):
        if not t['valid']: continue
        a=alpha[c];m=t['mask'];w=t['weights']
        area=(m*w).sum().clamp_min(20)
        occupancy=((a-m).square()*w).sum()/area
        # Soft predicted contour, in original-pixel distance units.
        edge=torch.nn.functional.pad((a[:,1:]-a[:,:-1]).abs(),(0,1))
        edge=edge+torch.nn.functional.pad((a[1:]-a[:-1]).abs(),(0,0,0,1))
        boundary=(edge*w*(t['boundary_dt']/10).square()).sum()/(edge*w).sum().clamp_min(1)
        terms.append(occupancy+0.1*boundary)
    return torch.stack(terms).mean() if terms else pred_2d_v.sum()*0


def mask_cover_loss(pred_2d_v, cover_pts, targets=None):
    """Two-sided complement of mask_align_loss: every sampled inside-mask pixel should
    be near some projected vert. Pixels farther than cover_max_px from all verts are
    dropped (forearm/cuff area outside MANO coverage).
    pred_2d_v (NUM_CAMS,V,2) px; cover_pts list of (M,2) or None."""
    terms = []
    for cam_idx in range(NUM_CAMS):
        cp = cover_pts[cam_idx]
        if cp is None:
            continue
        d = torch.cdist(cp, pred_2d_v[cam_idx]).min(dim=1).values  # (M,) px
        keep = d < cover_max_px
        if int(keep.sum()) > 0:
            weight = wrist_weights(cp, targets[cam_idx]['wrist']) if targets else torch.ones_like(d)
            terms.append(((d[keep] ** 2)*weight[keep]).sum()/weight[keep].sum().clamp_min(1))
    if not terms:
        return pred_2d_v.sum() * 0.0
    return torch.stack(terms).mean()


def mask_align_loss(pred_2d_v, dt_maps, targets=None):
    """One-sided silhouette term: squared DT px of projected verts outside the mask.
    pred_2d_v (3,V,2) px; dt_maps list of (1,1,H,W) or None."""
    terms = []
    for cam_idx in range(NUM_CAMS):
        if dt_maps[cam_idx] is None:
            continue
        uv = pred_2d_v[cam_idx]
        grid = torch.stack([uv[:, 0] / (RES[0] - 1) * 2 - 1,
                            uv[:, 1] / (RES[1] - 1) * 2 - 1], dim=-1)[None, None]  # (1,1,V,2)
        d = torch.nn.functional.grid_sample(dt_maps[cam_idx], grid, mode='bilinear',
                                            padding_mode='border', align_corners=True)[0, 0, 0]
        inb = (uv[:, 0] >= 0) & (uv[:, 0] < RES[0]) & (uv[:, 1] >= 0) & (uv[:, 1] < RES[1])
        if int(inb.sum()) > 0:
            weight = wrist_weights(uv,targets[cam_idx]['wrist']) if targets else torch.ones_like(d)
            terms.append(((d[inb] ** 2)*weight[inb]).sum()/weight[inb].sum().clamp_min(1))
    if not terms:
        return pred_2d_v.sum() * 0.0
    return torch.stack(terms).mean()


def compute_loss(verts_wld, joints_wld, params, T_w2c, gt_2d, gt_valid, gt_2d_hamba, gt_valid_hamba, prev_params, check_2d_validity=False, validity=None, obj_pts=None, hand_faces=None, dt_maps=None, cover_pts=None, temp_shift=None, silhouette_data=None, silhouette_active=False):

    loss_fn = torch.nn.MSELoss()

    # translate verts
    ones = torch.ones((verts_wld.shape[0], 1), dtype=verts_wld.dtype, device=verts_wld.device)
    vertices_world_hom = torch.cat([verts_wld, ones], dim=1)

    # translate joints
    ones = torch.ones((joints_wld.shape[0], 1), dtype=joints_wld.dtype, device=joints_wld.device)
    joints_world_hom = torch.cat([joints_wld, ones], dim=1)

    pred_2d_v = []
    pred_2d_j = []
    pred_z = []
    loss = {}

    # need to change it to batch operation
    for cam_idx in range(NUM_CAMS):
        # Apply inverse transform
        vertices_hom = (T_w2c[cam_idx] @ vertices_world_hom.T).T  # (N, 4)
        joints_hom = (T_w2c[cam_idx] @ joints_world_hom.T).T  # (N, 4)

        # Optionally extract 3D camera coordinates
        vertices_cam = vertices_hom[:, :3]
        joints_cam = joints_hom[:, :3]
        pred_z.append(vertices_cam[:,2])

        pred_2d_v.append(hamba_3d_to_2d(vertices_cam, cam_idx=cam_idx)[None])
        pred_2d_j.append(hamba_3d_to_2d(joints_cam, cam_idx=cam_idx)[None])

    pred_2d_v = torch.cat(pred_2d_v, 0)
    pred_2d_j = torch.cat(pred_2d_j, 0)

    if validity is not None:
        mp_val = torch.Tensor(np.asarray(validity[0]).astype(np.float32)).to(gt_2d.device)
        gt_valid_ = gt_valid * mp_val[:, None, None]

        hamba_weight_ = torch.Tensor(np.asarray([hamba_weight,] * sum(gt_valid_hamba))).to(gt_2d.device)
        hamba_val = torch.Tensor(np.asarray(validity[1]).astype(np.float32)).to(gt_2d.device) 
        hamba_weight_ = hamba_weight_ * hamba_val
        hamba_weight_ = hamba_weight_[:, None, None]

    else:
        gt_valid_ = gt_valid
        hamba_weight_ = hamba_weight
       
    if hamba_weight > 0:
        loss['2d'] = (2*args.robust_joint_delta*torch.nn.functional.smooth_l1_loss(pred_2d_j*gt_valid_,gt_2d*gt_valid_,beta=args.robust_joint_delta) if args.robust_joint_delta>0 else loss_fn(pred_2d_j*gt_valid_,gt_2d*gt_valid_))
    else:
        loss['2d'] = (2*args.robust_joint_delta*torch.nn.functional.smooth_l1_loss(pred_2d_j,gt_2d,beta=args.robust_joint_delta) if args.robust_joint_delta>0 else loss_fn(pred_2d_j,gt_2d))

    if sum(gt_valid_hamba) > 0:
        loss['2d_hamba'] = loss_fn(pred_2d_v[gt_valid_hamba] * hamba_weight_, gt_2d_hamba * hamba_weight_)

    if w_pen > 0 and obj_pts is not None and hand_faces is not None:
        loss['pen'] = w_pen * penetration_loss(verts_wld, hand_faces, obj_pts, pen_band_mm / 1000.0)

    if w_mask > 0 and dt_maps is not None:
        loss['mask'] = w_mask * mask_align_loss(pred_2d_v, dt_maps, silhouette_data)

    if w_mask_cover > 0 and cover_pts is not None:
        loss['mask_cover'] = w_mask_cover * mask_cover_loss(pred_2d_v, cover_pts, silhouette_data)

    if args.w_silhouette > 0 and silhouette_active and silhouette_data is not None:
        loss['silhouette'] = args.w_silhouette * args.silhouette_every * rendered_silhouette_loss(pred_2d_v, pred_z, hand_faces, silhouette_data)

    if prev_params is not None:
        '''
        if loss['2d'].item() < 20:
            temp_weight = 1e+7 
        elif loss['2d'].item() < 30:
            temp_weight = 5e+5 
        elif loss['2d'].item() < 40:
            temp_weight = 1e+4
        else:
            temp_weight = 1e+2
        '''
        #temp_weight = args.temp_weight #5e+5

        for key in prev_params.keys():
            if key == 'gt_valid':
                continue

            elif key == 'verts':
                _tgt = torch.Tensor(prev_params[key]).to(device)
                if temp_shift is not None:
                    # ball-anchor: rigid hold => the hand moves WITH the ball this frame
                    _tgt = _tgt + torch.Tensor(temp_shift).to(device)[None]
                loss[f'temp_{key}'] = loss_fn(verts_wld, _tgt) * temp_weight

            elif key == 'hand_pose':
                pose_weight = temp_weight if args.pose_temp_weight is None else args.pose_temp_weight
                loss[f'temp_{key}'] = loss_fn(params[key], prev_params[key]) * pose_weight

            #if prev_params['gt_valid'] != gt_valid and f'temp_{key}' in loss.keys():
            #    loss[f'temp_{key}'] *= 1e2

        if check_2d_validity:
            mp_valid = []
            hamba_valid = []

            for cam_idx in range(NUM_CAMS):
                l = loss_fn(pred_2d_j[cam_idx][None] * gt_valid[cam_idx], gt_2d[cam_idx][None] * gt_valid[cam_idx]).item()
                if l > 80:
                    mp_valid.append(False)
                else:
                    mp_valid.append(True)

            for hamba_idx in range(sum(gt_valid_hamba)):
                l = loss_fn(pred_2d_v[gt_valid_hamba][hamba_idx][None], gt_2d_hamba[hamba_idx][None]).item() * hamba_weight

                if l > 60:
                    hamba_valid.append(False)
                else:
                    hamba_valid.append(True)

            return loss, (mp_valid, hamba_valid)

    return loss, None


def shape_input_identity():
    """Bind a reusable calibration to observations, model, implementation and fit weights."""
    case = Path(phystwin_base_path)/case_name
    files = [case/'metadata.json',case/'calibrate.pkl',Path(__file__),Path(args.observations_cache or case/log/'observations.pkl')]
    files += sorted((case/'color').glob('*/*.png'))
    files += sorted((case/'mask').glob('*/*/*.png')) + sorted((case/'mask').glob('mask_info_*.json'))
    files += sorted((Path(hamba_base_path)/case_name).glob('*/results.pt'))
    files += [Path.cwd()/'data_process/models/mano_wrapper.py']
    files += sorted((Path.cwd()/'data_process/Hamba/_DATA/data/mano').glob('*.pkl'))
    return dict(algorithm=ALGORITHM_VERSION,seed=args.seed,num_cams=NUM_CAMS,num_hands=num_hands,
        weights=dict(w_mask=w_mask,w_mask_cover=w_mask_cover,wrist_weight=args.wrist_weight,hamba_weight=hamba_weight,cover_max_px=cover_max_px),
        hashes={str(p):file_digest(p) for p in files})


def representative_shape_frames(frame_num,hand_data,mp_out,is_right,ref):
    """Select by measured mask/reprojection agreement and articulation diversity.
    MediaPipe handedness confidence is used only as an availability gate.
    """
    hidx=1 if is_right else 0;side='Left' if is_right else 'Right'
    if args.shape_frames:
        frames=sorted(set(int(x) for x in args.shape_frames.split(',') if x.strip()))
        if not frames or any(f<0 or f>=frame_num or hand_data[ref].get(f'{f}_{hidx}') is None for f in frames):
            raise ValueError('Invalid explicit calibration frames')
        return frames
    ids=HAND_MASK_IDS if num_hands==1 else _two_hand_mask_ids(is_right,mp_out)
    # Extrinsics are supplied by process_hands through the selection context.
    projections=[np.asarray(REAL_KS[c])@np.linalg.inv(SHAPE_SELECTION_C2WS[c])[:3] for c in range(NUM_CAMS)]
    candidates=[]
    for f in range(frame_num):
        if hand_data[ref] is None or hand_data[ref].get(f'{f}_{hidx}') is None:continue
        views=[];inliers=[]
        for c in range(NUM_CAMS):
            if float(np.asarray(mp_out[c]['conf'][side][f]).mean())<.5:continue
            xy=np.asarray(mp_out[c]['kpts'][side][f],dtype=float).reshape(21,2)
            mask=cv2.imread(f'{phystwin_base_path}/{case_name}/mask/{c}/{ids[c]}/{f}.png',0)
            if mask is None or (mask>127).sum()<50:continue
            padded=cv2.dilate((mask>127).astype(np.uint8),np.ones((11,11),np.uint8))
            ij=np.rint(xy).astype(int);inside=(ij[:,0]>=0)&(ij[:,0]<mask.shape[1])&(ij[:,1]>=0)&(ij[:,1]<mask.shape[0])
            ok=np.zeros(21,dtype=bool);ok[inside]=padded[ij[inside,1],ij[inside,0]]>0
            if ok.mean()>=.8:views.append((c,xy));inliers.append(float(ok.mean()))
        if len(views)<2:continue
        xyz=[];errors=[]
        for j in range(21):
            rows=[]
            for c,xy in views:
                P=projections[c];rows.extend([xy[j,0]*P[2]-P[0],xy[j,1]*P[2]-P[1]])
            _,_,vh=np.linalg.svd(np.asarray(rows));X=vh[-1]
            if abs(X[3])<1e-8:break
            X=X/X[3];xyz.append(X[:3])
            for c,xy in views:
                q=projections[c]@X
                errors.append(float(np.linalg.norm(q[:2]/q[2]-xy[j])))
        if len(xyz)!=21:continue
        error=float(np.median(errors))
        if error>10:continue
        xyz=np.asarray(xyz);descriptor=[]
        for base in [1,5,9,13,17]:
            bones=np.diff(xyz[[0,base,base+1,base+2,base+3]],axis=0)
            bones/=np.maximum(np.linalg.norm(bones,axis=1,keepdims=True),1e-8)
            descriptor.extend(np.sum(bones[:-1]*bones[1:],axis=1))
        candidates.append(dict(frame=f,error=error,views=len(views),mask_agreement=float(np.mean(inliers)),descriptor=np.asarray(descriptor)))
    if len(candidates)<3:raise RuntimeError('Too few geometrically consistent calibration frames')
    # Retain the better geometric observations, then farthest-point sample pose descriptors.
    candidates.sort(key=lambda x:x['error']+2*(NUM_CAMS-x['views'])+5*(1-x['mask_agreement']))
    pool=candidates[:max(3,int(np.ceil(len(candidates)*.75)))];chosen=[pool.pop(0)]
    while pool and len(chosen)<args.shape_frame_count:
        best=max(range(len(pool)),key=lambda i:min(float(np.linalg.norm(pool[i]['descriptor']-x['descriptor'])) for x in chosen))
        chosen.append(pool.pop(best))
    print('Geometry/diversity calibration selection:',[{k:v for k,v in x.items() if k!='descriptor'} for x in chosen],flush=True)
    return sorted(x['frame'] for x in chosen)


def calibrate_shared_beta(frames,hand_data,mp_out,c2ws,is_right,ref):
    """One beta shared across frames and views; independent frame poses, no temporal loss."""
    side='Left' if is_right else 'Right';hidx=1 if is_right else 0
    records=[hand_data[ref][f'{f}_{hidx}'] for f in frames]
    initial=hand_data[ref][f'0_{hidx}']['pred_mano_params']['betas'].detach().clone().to(device)
    beta=initial.clone().requires_grad_(True)
    global_rot=torch.cat([r['pred_mano_params']['global_orient'].detach().to(device) for r in records],0)
    local_rot=torch.cat([r['pred_mano_params']['hand_pose'].detach().to(device) for r in records],0)
    rg=matrix_to_rotation_6d(global_rot).clone().requires_grad_(True)
    rh=matrix_to_rotation_6d(local_rot).clone().requires_grad_(True)
    translation=torch.cat([torch.as_tensor(r['pred_cam_t_org'],dtype=torch.float32,device=device).reshape(1,3) for r in records],0).requires_grad_(True)
    gt=torch.as_tensor(np.asarray([[mp_out[c]['kpts'][side][f] for c in range(NUM_CAMS)] for f in frames]),dtype=torch.float32,device=device)
    confidence=torch.as_tensor(np.asarray([[np.asarray(mp_out[c]['conf'][side][f]).reshape(21,-1)[:,:1] for c in range(NUM_CAMS)] for f in frames]),dtype=torch.float32,device=device)
    c2w=torch.as_tensor(np.asarray(c2ws),dtype=torch.float32,device=device)
    w2c=torch.linalg.inv(c2w)
    ks=torch.as_tensor(np.asarray(REAL_KS),dtype=torch.float32,device=device)
    ids=HAND_MASK_IDS if num_hands==1 else _two_hand_mask_ids(is_right,mp_out)
    targets=[silhouette_targets(f,ids,mp_out,is_right) for f in frames]
    dts=[];points=[];cover_valid=[]
    for f in frames:
        fd=[];fp=[];fv=[]
        for c in range(NUM_CAMS):
            m=cv2.imread(f'{phystwin_base_path}/{case_name}/mask/{c}/{ids[c]}/{f}.png',0)>127
            ys,xs=np.nonzero(m)
            good=len(ys)>=50
            fd.append(cv2.distanceTransform((~m).astype(np.uint8),cv2.DIST_L2,3) if good else np.zeros(m.shape,np.float32))
            if good:
                index=np.linspace(0,len(ys)-1,256).astype(int);fp.append(np.stack([xs[index],ys[index]],1).astype(np.float32))
            else:fp.append(np.zeros((256,2),np.float32))
            fv.append(good)
        dts.append(fd);points.append(fp);cover_valid.append(fv)
    dt=torch.as_tensor(np.asarray(dts),device=device).reshape(-1,1,int(RES[1]),int(RES[0]))
    cp=torch.as_tensor(np.asarray(points),device=device)
    valid_mask=torch.as_tensor(cover_valid,device=device)
    cp_weight=torch.stack([torch.stack([wrist_weights(cp[k,c],targets[k][c]['wrist']) for c in range(NUM_CAMS)]) for k in range(len(frames))])
    optimizer=torch.optim.AdamW([{'params':[rg,rh,translation],'lr':2e-3,'weight_decay':.01},
                                  {'params':[beta],'lr':args.shape_lr,'weight_decay':0.0}])
    history=[]
    for step in range(args.shape_iters):
        optimizer.zero_grad()
        pred=mano(global_orient=rotation_6d_to_matrix(rg),hand_pose=rotation_6d_to_matrix(rh),betas=beta.expand(len(frames),-1),pose2rot=False)
        vertices=pred.vertices;joints=pred.joints
        if not is_right:
            sign=torch.tensor([-1.,1.,1.],device=device);vertices=vertices*sign;joints=joints*sign
        def project_batch(p):
            local=p+translation[:,None]
            world=local@c2w[ref,:3,:3].T+c2w[ref,:3,3]
            camera=torch.einsum('kvi,cji->kcvj',world,w2c[:,:3,:3])+w2c[None,:,None,:3,3]
            pixels=torch.einsum('kcvi,cji->kcvj',camera,ks)
            return pixels[...,:2]/pixels[...,2:].clamp_min(1e-5)
        uv=project_batch(vertices);jv=project_batch(joints)
        joint=((jv-gt)*confidence).square().mean()
        grid=torch.stack([uv[...,0]/(RES[0]-1)*2-1,uv[...,1]/(RES[1]-1)*2-1],-1).reshape(-1,1,778,2)
        distance=torch.nn.functional.grid_sample(dt,grid,align_corners=True,padding_mode='border').reshape(len(frames),NUM_CAMS,778)
        # Detach spatial weights so shape cannot lower its loss merely by moving into a low-weight region.
        vw=torch.stack([torch.stack([wrist_weights(uv[k,c].detach(),targets[k][c]['wrist']) for c in range(NUM_CAMS)]) for k in range(len(frames))])
        vw=vw*valid_mask[:,:,None]
        outside=(distance.square()*vw).sum()/vw.sum().clamp_min(1)
        nearest=torch.cdist(cp,uv).min(-1).values
        keep=(nearest<cover_max_px)&valid_mask[:,:,None]
        cw=cp_weight*keep
        cover=(nearest.square()*cw).sum()/cw.sum().clamp_min(1)
        prior=beta.square().mean()+0.1*(beta-initial).square().mean()
        loss=joint+w_mask*outside+w_mask_cover*cover+args.shape_prior_weight*prior
        loss.backward()
        # Stabilize per-frame pose/translation before allowing shape changes.
        if step<min(500,args.shape_iters//3):beta.grad.zero_()
        torch.nn.utils.clip_grad_norm_([beta],10.0)
        optimizer.step()
        with torch.no_grad():beta.copy_(torch.maximum(torch.minimum(beta,initial+args.shape_max_delta),initial-args.shape_max_delta))
        if step%250==0 or step==args.shape_iters-1:
            row=dict(step=step,loss=float(loss.detach()),joint=float(joint.detach()),outside=float(outside.detach()),cover=float(cover.detach()),prior=float(prior.detach()),beta_delta_max=float((beta-initial).abs().max().detach()))
            history.append(row);print('shared shape',side,row,flush=True)
    if not torch.isfinite(beta).all():raise RuntimeError('Non-finite shared shape')
    # Guard against drastic bone-length changes; this is a numerical safety bound, not anatomical validation.
    with torch.no_grad():
        eye=torch.eye(3,device=device)
        rest=mano(global_orient=eye[None,None].expand(2,1,3,3),hand_pose=eye[None,None].expand(2,15,3,3),betas=torch.cat([initial,beta.detach()]),pose2rot=False).joints
        edges=torch.tensor([[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[0,9],[9,10],[10,11],[11,12],[0,13],[13,14],[14,15],[15,16],[0,17],[17,18],[18,19],[19,20]],device=device)
        lengths=(rest[:,edges[:,1]]-rest[:,edges[:,0]]).norm(dim=-1)
        ratios=lengths[1]/lengths[0].clamp_min(1e-6)
        if (ratios<.9).any() or (ratios>1.1).any():raise RuntimeError('Shared shape exceeds 10% bone-length change guard')
    return beta.detach().cpu().numpy(),dict(frames=frames,initial_betas=initial.cpu().numpy().tolist(),betas=beta.detach().cpu().numpy().tolist(),history=history,bone_length_ratio_min=float(ratios.min()),bone_length_ratio_max=float(ratios.max()))


def prepare_shared_shape(frame_num,hand_data,mp_out,c2ws,ref_cidx):
    global SHARED_BETAS, SHAPE_SELECTION_C2WS
    SHAPE_SELECTION_C2WS=np.asarray(c2ws)
    SHARED_BETAS={}
    if not (args.shared_shape or args.shared_shape_file):return None
    identity=shape_input_identity()
    path=Path(args.shared_shape_file or f'{phystwin_base_path}/{case_name}/{log}/shared_shape.pkl')
    settings={k:getattr(args,k) for k in ('shape_frames','shape_frame_count','shape_iters','shape_lr','shape_prior_weight','shape_max_delta')}
    if path.exists():
        with path.open('rb') as f:stored=pickle.load(f)
        if stored['identity']!=identity:raise RuntimeError('Shared shape input/code/model mismatch; use a new calibration')
        if not args.shared_shape_file and stored['settings']!=settings:raise RuntimeError('Shared shape settings changed; use a new attempt')
    else:
        if args.shared_shape_file:raise FileNotFoundError(path)
        stored=dict(identity=identity,settings=settings,betas={},diagnostics={})
        for hidx in ([1] if num_hands==1 else [0,1]):
            is_right=hidx==1;side='right' if is_right else 'left'
            frames=representative_shape_frames(frame_num,hand_data,mp_out,is_right,ref_cidx[hidx])
            beta,diagnostic=calibrate_shared_beta(frames,hand_data,mp_out,c2ws,is_right,ref_cidx[hidx])
            stored['betas'][side]=beta;stored['diagnostics'][side]=diagnostic
        path.parent.mkdir(parents=True,exist_ok=True)
        tmp=path.with_suffix('.tmp')
        with tmp.open('wb') as f:pickle.dump(stored,f)
        os.replace(tmp,path)
        path.with_suffix('.json').write_text(json.dumps(dict(settings=settings,diagnostics=stored['diagnostics']),indent=2))
    for side,beta in stored['betas'].items():SHARED_BETAS[side=='right']=torch.as_tensor(beta,dtype=torch.float32,device=device).detach()
    print('Shared beta calibrated and frozen:',str(path),flush=True)
    return path


HAND_MASK_SIDE_CACHE = {}
def _two_hand_mask_ids(is_right, mp_out):
    """Two-hand cases: per-cam mask id for THIS side. SAM ids are persistent per cam video,
    so match ONCE per (cam, side): MediaPipe keypoint centroid (first confident frame) vs
    the two hand-mask centroids; cache the winner."""
    side_key = 'R' if is_right else 'L'
    mp_side = 'Left' if is_right else 'Right'   # mediapipe side naming is mirrored
    out = {}
    for cidx in range(NUM_CAMS):
        ck = (cidx, side_key)
        if ck not in HAND_MASK_SIDE_CACHE:
            info = json.load(open(f"{phystwin_base_path}/{case_name}/mask/mask_info_{cidx}.json"))
            hand_ids = [k for k, v in info.items() if "hand" in v]
            chosen = hand_ids[0]
            n_f = len(mp_out[cidx]['kpts'][mp_side])
            for f in range(n_f):
                conf = np.asarray(mp_out[cidx]['conf'][mp_side][f]).astype(float)
                if np.mean(conf) < 0.5:
                    continue
                kc = np.nanmean(np.asarray(mp_out[cidx]['kpts'][mp_side][f]), axis=0)
                best, bd = None, 1e18
                for mid in hand_ids:
                    m = cv2.imread(f"{phystwin_base_path}/{case_name}/mask/{cidx}/{mid}/{f}.png",
                                   cv2.IMREAD_GRAYSCALE)
                    if m is None or (m > 127).sum() < 50:
                        continue
                    ys, xs = np.nonzero(m > 127)
                    d = (xs.mean() - kc[0]) ** 2 + (ys.mean() - kc[1]) ** 2
                    if d < bd:
                        bd, best = d, mid
                if best is not None:
                    chosen = best
                    break
            HAND_MASK_SIDE_CACHE[ck] = chosen
            print(f"[two-hand mask] cam{cidx} side={side_key} -> mask id {chosen}")
        out[cidx] = HAND_MASK_SIDE_CACHE[ck]
    return out


def align_initial_camera(params, targets, valid, c2w, w2c, ref, is_right, fidx):
    """Align only global rotation/translation; shape and finger pose stay fixed."""
    cams = [c for c in range(NUM_CAMS) if bool(valid[c])]
    if not cams or REAL_KS is None:
        return params
    target = targets.detach()
    if not torch.isfinite(target).all():
        raise ValueError('Non-finite HaMBA camera initialization targets')
    rotation = matrix_to_rotation_6d(params['global_orient'].detach()).clone().requires_grad_(True)
    translation = params['transl'].detach().clone().requires_grad_(True)
    finger = params['hand_pose'].detach().clone()
    beta = params['betas'].detach().clone()
    opt = torch.optim.Adam([{'params':[rotation], 'lr':0.003}, {'params':[translation], 'lr':0.001}])
    def evaluate():
        pred = mano(global_orient=rotation_6d_to_matrix(rotation), hand_pose=finger, betas=beta, pose2rot=False)
        v = pred.vertices[0]
        if not is_right:
            v = v * v.new_tensor([-1., 1., 1.])
        v = v + translation
        world = v @ c2w[ref,:3,:3].T + c2w[ref,:3,3]
        pixels = []
        for c in cams:
            q = world @ w2c[c,:3,:3].T + w2c[c,:3,3]
            if bool((q[:,2] <= 0.01).any()):
                return None, world
            pixels.append(hamba_3d_to_2d(q, cam_idx=c))
        return torch.stack(pixels)-target, world
    best = None; initial = None; before_world = None
    for step in range(args.camera_init_iters+1):
        opt.zero_grad()
        diff, world = evaluate()
        if diff is None: break
        loss = diff.square().sum(-1).mean()
        if not torch.isfinite(loss): break
        value = float(loss.detach())
        if initial is None:
            initial = value; before_world = world.detach().cpu().numpy()
        if best is None or value < best[0]:
            best = (value, rotation.detach().clone(), translation.detach().clone(), world.detach().cpu().numpy())
        if step == args.camera_init_iters: break
        loss.backward(); opt.step()
    if best is None:
        raise ValueError('Initial MANO hand is behind a participating camera')
    params['global_orient'] = rotation_6d_to_matrix(best[1]).detach().requires_grad_(True)
    params['transl'] = best[2].detach().requires_grad_(True)
    folder = Path(phystwin_base_path)/case_name/log
    folder.mkdir(parents=True,exist_ok=True)
    tag = f'camera_init_{fidx:03d}_{int(is_right)}'
    (folder/(tag+'.json')).write_text(json.dumps(dict(frame=fidx,cameras=cams,iterations=args.camera_init_iters,before_rmse_px=initial**0.5,after_rmse_px=best[0]**0.5,shape_and_finger_pose_fixed=True),indent=2))
    np.savez(folder/(tag+'.npz'),before=before_world,after=best[3])
    print(f'[camera init] f{fidx}: {initial**0.5:.3f} -> {best[0]**0.5:.3f}px',flush=True)
    return params


def register(frame_data, c2ws, is_right, prev_params, ref_cidx, fidx, mp_out, obj_pts=None):

    T_c2w = c2ws
    T_w2c = [np.linalg.inv(x) for x in T_c2w]

    hand_faces = torch.tensor(FACES['right'] if is_right else FACES['left'],
                              dtype=torch.int64, device=device)
    obj_pts_t = torch.tensor(obj_pts, dtype=torch.float32, device=device) if obj_pts is not None else None
    _mask_ids = HAND_MASK_IDS
    if HAND_MASK_IDS is not None and num_hands == 2:
        _mask_ids = _two_hand_mask_ids(is_right, mp_out)
    silhouette_data = silhouette_targets(fidx,_mask_ids,mp_out,is_right) if (_mask_ids is not None and (args.w_silhouette > 0 or args.wrist_weight < 1)) else None
    dt_maps = mask_dt_maps(fidx, _mask_ids) if (w_mask > 0 and _mask_ids is not None) else None
    cover_pts = mask_cover_pts(fidx, ids=_mask_ids) if (w_mask_cover > 0 and _mask_ids is not None) else None
    _ball_shift = ball_temp_shift(fidx, is_right)
    if _ball_shift is not None:
        print(f"[ball-anchor] f{fidx} {'R' if is_right else 'L'} temp target shifted by "
              f"{np.round(np.asarray(_ball_shift) * 1000, 1).tolist()} mm")

    T_c2w, T_w2c = torch.Tensor(T_c2w).to(device), torch.Tensor(T_w2c).to(device)

    gt_2d_hamba, gt_valid_hamba = compute_hamba_2d_gt(frame_data, is_right)

    if num_hands == 1:
        gt_2d = np.concatenate([mp_out[i]['kpts']['Left'][fidx][None] for i in range(NUM_CAMS)], 0)
        gt_valid = np.concatenate([mp_out[i]['conf']['Left'][fidx][None] for i in range(NUM_CAMS)], 0)

    else:
        side = 'Left' if is_right else 'Right' # mediapipe side is the opposite
        gt_2d = np.concatenate([mp_out[i]['kpts'][side][fidx][None] for i in range(NUM_CAMS)], 0)
        gt_valid = np.concatenate([mp_out[i]['conf'][side][fidx][None] for i in range(NUM_CAMS)], 0)

    gt_2d_hamba = torch.Tensor(gt_2d_hamba).to(device)
    gt_valid_hamba = torch.Tensor(gt_valid_hamba).to(device).bool()

    gt_2d = torch.Tensor(gt_2d).to(device)
    gt_valid = torch.Tensor(gt_valid).to(device)

    # --- Initialize parameters ---
    if prev_params is None:
        pred_mano_params_ = frame_data[ref_cidx]['pred_mano_params']
        camera_translation = torch.Tensor(frame_data[ref_cidx]['pred_cam_t_org']).to(device)
        pred_mano_params = {
            'global_orient': pred_mano_params_['global_orient'].clone().requires_grad_(True).to(device),
            'hand_pose': pred_mano_params_['hand_pose'].clone().requires_grad_(True).to(device),
            'betas': pred_mano_params_['betas'].clone().to(device),
            'transl': camera_translation.clone().to(device).requires_grad_(True),
        }
    else:
        pred_mano_params = {
            'global_orient': prev_params['global_orient'].detach().clone().requires_grad_(True).to(device),
            'hand_pose': prev_params['hand_pose'].detach().clone().requires_grad_(True).to(device),
            'transl': prev_params['transl'].detach().clone().requires_grad_(True).to(device),
            'betas': prev_params['betas'].to(device),
        }

    if is_right in SHARED_BETAS and (prev_params is None or not args.fit_shape):
        pred_mano_params['betas'] = SHARED_BETAS[is_right].detach().clone()

    if prev_params is None and args.camera_init_iters > 0:
        pred_mano_params = align_initial_camera(pred_mano_params, gt_2d_hamba, gt_valid_hamba, T_c2w, T_w2c, ref_cidx, is_right, fidx)

    # --- Set up optimizer ---
    initial_pose = pred_mano_params['hand_pose'].detach().clone()
    rot_global = matrix_to_rotation_6d(pred_mano_params['global_orient'].detach()).clone().requires_grad_(True)
    rot_hand = matrix_to_rotation_6d(pred_mano_params['hand_pose'].detach()).clone().requires_grad_(True)
    beta_start = pred_mano_params['betas'].detach().clone()
    pred_mano_params['betas'] = beta_start.clone().requires_grad_(args.fit_shape)
    params = [{'params': [rot_global, rot_hand, pred_mano_params['transl']], 'lr': 2e-3}]
    if args.fit_shape:
        params.append({'params': [pred_mano_params['betas']], 'lr': args.fit_shape_lr, 'weight_decay': 0.0})
    optimizer = torch.optim.AdamW(params, lr=2e-3)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=40, gamma=0.98)

    # --- Optimization loop ---
    #num_iters = 1500 #1700

    validity = None

    for i in range(num_iters):
        optimizer.zero_grad()

        pred_mano_params['global_orient'] = rotation_6d_to_matrix(rot_global)
        pred_mano_params['hand_pose'] = rotation_6d_to_matrix(rot_hand)
        # Forward pass through your MANO model
        pred = mano(
            global_orient=pred_mano_params['global_orient'],
            hand_pose=pred_mano_params['hand_pose'],
            betas=pred_mano_params['betas'],
            pose2rot=False
        )

        vertices = pred.vertices[0]
        joints = pred.joints[0]

        if not is_right:
            vertices[:, 0] *= -1. 
            joints[:, 0] *= -1. 

        vertices += (pred_mano_params['transl'])
        joints += (pred_mano_params['transl'])

        # translate verts
        ones = torch.ones((vertices.shape[0], 1), dtype=vertices.dtype, device=vertices.device)
        vertices_hom = torch.cat([vertices, ones], dim=1)  # (N, 4)

        vertices_world_hom = (T_c2w[ref_cidx] @ vertices_hom.T).T  # (N, 4)
        vertices_world = vertices_world_hom[:, :3]  # drop homogeneous coordinate
        
        # translate joints
        ones = torch.ones((joints.shape[0], 1), dtype=joints.dtype, device=joints.device)
        joints_hom = torch.cat([joints, ones], dim=1)  # (N, 4)

        joints_world_hom = (T_c2w[ref_cidx] @ joints_hom.T).T  # (N, 4)
        joints_world = joints_world_hom[:, :3]  # drop homogeneous coordinate

        loss_dict, val = compute_loss(vertices_world, joints_world, pred_mano_params, T_w2c, gt_2d, gt_valid, gt_2d_hamba, gt_valid_hamba, prev_params, check_2d_validity=(i==0), validity=validity, obj_pts=obj_pts_t, hand_faces=hand_faces, dt_maps=dt_maps, cover_pts=cover_pts, temp_shift=_ball_shift, silhouette_data=silhouette_data, silhouette_active=(i % args.silhouette_every == 0))
        if args.fit_shape and prev_params is not None and args.fit_shape_temporal_weight > 0:
            loss_dict['shape_temporal'] = args.fit_shape_temporal_weight * (pred_mano_params['betas'] - prev_params['betas'].detach()).square().mean()
        if args.w_pose_prior > 0:
            loss_dict['pose_prior'] = args.w_pose_prior * (pred_mano_params['hand_pose']-initial_pose).square().mean()

        if (val is not None) and (case_name in filter_list):
            validity = val

        loss = sum(loss_dict.values())

        # Backprop + optimize
        loss.backward()
        if args.staged_fit and rot_hand.grad is not None:
            if i < int(num_iters*0.10):
                rot_hand.grad.zero_()
            elif i < int(num_iters*0.25):
                active = torch.zeros((15,),device=device)
                active[[0,3,6,9,12]] = 1
                rot_hand.grad.mul_(active[None,:,None])
        optimizer.step()
        if args.fit_shape and args.fit_shape_max_delta > 0:
            with torch.no_grad():
                pred_mano_params['betas'].copy_(beta_start + (pred_mano_params['betas']-beta_start).clamp(-args.fit_shape_max_delta, args.fit_shape_max_delta))

        if scheduler.get_last_lr()[0] > 2.5e-4: 
            scheduler.step()

        if i % 100 == 0:
            print(f"[{i}/{num_iters}] LR: {scheduler.get_last_lr()[0]} Loss: {loss_dict}, {validity}")

    # Save the final optimizer state, never the pre-step forward result.
    with torch.no_grad():
        pred_mano_params['betas'] = pred_mano_params['betas'].detach()
        if not torch.isfinite(pred_mano_params['betas']).all():
            raise RuntimeError('Non-finite fitted shape; refusing to save')
        pred_mano_params['global_orient'] = rotation_6d_to_matrix(rot_global).detach()
        pred_mano_params['hand_pose'] = rotation_6d_to_matrix(rot_hand).detach()
        for key in ('global_orient', 'hand_pose'):
            r=pred_mano_params[key]
            if not torch.isfinite(r).all() or (r.transpose(-1,-2)@r-torch.eye(3,device=device)).abs().max()>1e-4 or (torch.linalg.det(r)-1).abs().max()>1e-4:
                raise RuntimeError('Invalid rotation; refusing to save fit')
        final=mano(global_orient=pred_mano_params['global_orient'],hand_pose=pred_mano_params['hand_pose'],betas=pred_mano_params['betas'],pose2rot=False)
        vertices=final.vertices[0]
        if not is_right: vertices[:,0]*=-1
        vertices=vertices+pred_mano_params['transl']
        vertices_world=(T_c2w[ref_cidx]@torch.cat([vertices,torch.ones((len(vertices),1),device=device)],1).T).T[:,:3]
    if args.fit_shape:
        shape_folder = Path(phystwin_base_path)/case_name/log
        shape_folder.mkdir(parents=True, exist_ok=True)
        (shape_folder/f'shape_fit_{fidx:06d}_{int(is_right)}.json').write_text(json.dumps(dict(
            frame=fidx, is_right=bool(is_right), initial_betas=beta_start.cpu().numpy().tolist(),
            final_betas=pred_mano_params['betas'].cpu().numpy().tolist(),
            max_abs_change=float((pred_mano_params['betas']-beta_start).abs().max()),
            temporal_weight=args.fit_shape_temporal_weight), indent=2))
    verts_wld_translated = vertices_world.detach().cpu().numpy()
    pred_mano_params['verts'] = verts_wld_translated #[root_idx]
    pred_mano_params['gt_valid'] = gt_valid

    '''
    vertices_world_hom = np.hstack([verts_wld_translated, np.ones((verts_wld_translated.shape[0], 1))])  # (N, 4)

    # need to change it to batch operation
    for cam_idx in range(3):
        # Apply inverse transform
        vertices_hom = (T_w2c.cpu().numpy()[cam_idx] @ vertices_world_hom.T).T  # (N, 4)

        # Optionally extract 3D camera coordinates
        vertices_cam = vertices_hom[:, :3]

        points_2d = hamba_3d_to_2d(vertices_cam)

        image = cv2.imread(f"{phystwin_base_path}/{case_name}/color/{cam_idx}/0.png")  # BGR format
        output_image = image.copy()

        color = (0, 0, 255)

        # Loop over all points and set the pixel
        points = np.round(points_2d).astype(np.int32)

        for x, y in points:

            # Make sure the point is inside image bounds
            if 0 <= x < image.shape[1] and 0 <= y < image.shape[0]:
                output_image[y, x] = color  # Note: image[y, x], not [x, y]

        # Step 4: Save the modified image
        cv2.imwrite(f"../debug/debug_{cam_idx}_optim.png", output_image)
    import pdb; pdb.set_trace()
    '''
    return verts_wld_translated, pred_mano_params #, translation


def process_hands(case_name):
    timer = TimerLogger()
    timer.start("process_hands")
    timer.start("process_hands-read_data")

    base_path = osp.join(hamba_base_path, case_name)  
    hand_data = []

    for cam_idx in range(NUM_CAMS):
        hand_data_path = osp.join(base_path, str(cam_idx), 'results.pt')
        if osp.exists(hand_data_path):
            # [local] weights_only=False: torch>=2.6 flipped this default to True, which
            # rejects the numpy arrays inside results.pt. This file is our own stage-1
            # output (script_hand_recon.py), not a third-party checkpoint.
            hand_data.append(torch.load(hand_data_path, weights_only=False))
        else:
            print(f"no HaMBA results.pt for cam{cam_idx} — using it for MediaPipe/mask losses only")
            hand_data.append(None)

    #frame_num = len(hand_data[0].keys()) // 2
    # [local] split.json is written by TWO toolchains with different schemas under the same
    # name: KAIST's Mono expects {'frame_len': N}, while PhysTwin's run_physics.py writes
    # {'train': [0,K], 'test': [K,N]} (the physics train/test window). Prefer frame_len when
    # present, else fall back to metadata.json's frame_num, which is the authoritative total
    # frame count for the case and always exists.
    split_f = osp.join(f"{phystwin_base_path}/{case_name}/split.json")
    frame_num = None
    if osp.exists(split_f):
        _sp = json.load(open(split_f, 'r'))
        if 'frame_len' in _sp:
            frame_num = int(_sp['frame_len'])
    if frame_num is None:
        meta_f = osp.join(f"{phystwin_base_path}/{case_name}/metadata.json")
        frame_num = int(json.load(open(meta_f, 'r'))['frame_num'])
        print(f"split.json has no 'frame_len' -> frame_num={frame_num} from metadata.json")

    c2ws = pickle.load(open(f"{phystwin_base_path}/{case_name}/calibrate.pkl", "rb"))

    global REAL_KS
    meta_f = f"{phystwin_base_path}/{case_name}/metadata.json"
    if osp.exists(meta_f):
        REAL_KS = json.load(open(meta_f))["intrinsics"]
        print(f"using real per-cam intrinsics from {meta_f}: "
              + ", ".join(f"cam{c}(fx={K[0][0]:.1f} cx={K[0][2]:.1f} cy={K[1][2]:.1f})"
                          for c, K in enumerate(REAL_KS[:NUM_CAMS])))
    else:
        print(f"metadata.json not found ({meta_f}) — falling back to hardcoded K {fx}/{cx}/{cy}")

    if w_mask > 0 or w_mask_cover > 0 or args.w_silhouette > 0 or args.shared_shape or args.shared_shape_file:
        validate_hand_masks(frame_num)

    # object point cloud (world frame, per-frame) for the non-penetration loss
    obj_all = None
    if w_pen > 0:
        fd_path = f"{phystwin_base_path}/{case_name}/final_data.pkl"
        obj_all = pickle.load(open(fd_path, "rb"))['object_points']  # (T, N_obj, 3)

    # select the reference camera
    ref_cidx = {}

    for hidx in range(num_hands):
        
        if num_hands == 1: hidx = 1
        cidx = 0

        #frame_data = hand_data[cidx][f'0_{hidx}']

        for cidx in range(NUM_CAMS):
            if hand_data[cidx] is None or not f'0_{hidx}' in hand_data[cidx].keys():
                continue

            frame_data = hand_data[cidx][f'0_{hidx}']

            if frame_data is not None:
                ref_cidx[hidx] = cidx
                break

        if ref_cidx == 3:
            print('No prediction for the first frame...')
            return None, None

    print('ref_cidx: ', ref_cidx)

    os.makedirs(f"{phystwin_base_path}/{case_name}/{log}", exist_ok=True)

    out_path = f"{phystwin_base_path}/{case_name}/{log}/hand_reg.pkl"
    out_path_params = f"{phystwin_base_path}/{case_name}/{log}/hand_reg_params.pkl"
    out_path_cidx = f"{phystwin_base_path}/{case_name}/{log}/hand_reg_cidx.pkl"

    with open(out_path_cidx, 'wb') as f:
        pickle.dump(ref_cidx, f)

    out_dict = {}
    out_dict_params = {}

    prev_params = {}

    for hidx in range(num_hands):
        if num_hands == 1: hidx = 1
        prev_params[hidx] = None

    if args.init_from_log is not None and start_frame > 0:
        src = pickle.load(open(f"{phystwin_base_path}/{case_name}/{args.init_from_log}/hand_reg_params.pkl", "rb"))
        for hidx in range(num_hands):
            if num_hands == 1: hidx = 1
            is_right = 0 if (hidx == 0 and num_hands == 2) else 1
            side = 'right' if is_right else 'left'
            prev_params[hidx] = src[start_frame - 1][side]
        print(f"chained temporal init from {args.init_from_log} frame {start_frame - 1}")

    timer.end("process_hands-read_data")
    timer.start("process_hands-compute_mediapipe_2d_gt")

    mp_out = frozen_observations(frame_num)
    shared_shape_path = prepare_shared_shape(frame_num,hand_data,mp_out,c2ws,ref_cidx)
    if args.shape_only:
        print('Shared-shape setup complete; temporal tracking skipped',flush=True)
        return

    timer.end("process_hands-compute_mediapipe_2d_gt")

    try:
        render_hamba_videos(hand_data, frame_num)
    except Exception as e:
        print(f'hamba video render failed (non-fatal): {e}')

    timer.start("process_hands-optimize")

    loop_end = frame_num if end_frame < 0 else min(end_frame, frame_num)
    checkpoint_path = f"{phystwin_base_path}/{case_name}/{log}/fit_checkpoint.pkl"
    checkpoint_config = {k:v for k,v in vars(args).items() if k not in ('resume','stop_after_frames')}
    checkpoint_config['algorithm'] = ALGORITHM_VERSION
    case_path=Path(phystwin_base_path)/case_name
    input_files=[case_path/'metadata.json',case_path/'calibrate.pkl']+sorted((case_path/'color').glob('*/*.png'))+sorted((Path(hamba_base_path)/case_name).glob('*/results.pt'))
    for name in ('source_frames.json','frame_mapping.json','split.json'):
        if (case_path/name).exists(): input_files.append(case_path/name)
    if w_mask > 0 or w_mask_cover > 0 or args.w_silhouette > 0 or args.shared_shape or args.shared_shape_file:
        input_files += sorted((case_path/'mask').glob('mask_info_*.json'))
        input_files += sorted((case_path/'mask').glob('*/*/*.png'))
    input_files += [Path(__file__),Path.cwd()/'data_process/models/mano_wrapper.py']
    input_files += sorted((Path.cwd()/'data_process/Hamba/_DATA/data/mano').glob('*.pkl'))
    if args.init_from_log: input_files.append(case_path/args.init_from_log/'hand_reg_params.pkl')
    input_files.append(Path(args.observations_cache or case_path/log/'observations.pkl'))
    if shared_shape_path is not None:input_files.append(shared_shape_path)
    checkpoint_config['hashes']={str(x):file_digest(x) for x in input_files}
    if os.path.isfile(checkpoint_path) and not args.resume: raise RuntimeError('Existing checkpoint: use --resume or a new log directory')
    loop_start = start_frame
    if args.resume and os.path.isfile(checkpoint_path):
        with open(checkpoint_path, 'rb') as f:
            saved = pickle.load(f)
        if saved['config'] != checkpoint_config:
            raise RuntimeError('Checkpoint arguments differ; use a separate log directory.')
        if 'rng' not in saved: raise RuntimeError('Legacy checkpoint has no RNG state; use a new attempt')
        restore_rng(saved['rng'])
        out_dict, out_dict_params = saved['out'], saved['params']
        loop_start = saved['last_frame'] + 1
        for hidx in prev_params:
            side = 'right' if hidx == 1 else 'left'
            prev_params[hidx] = out_dict_params[saved['last_frame']][side]
        print(f'Resuming MANO fit at frame {loop_start}', flush=True)
    for fidx in tqdm(range(loop_start, loop_end)):
        
        out_dict[fidx] = {}
        out_dict_params[fidx] = {}
        vertices_world_reg = []

        for hidx in range(num_hands): 
            print(fidx, hidx)

            if num_hands == 1: hidx = 1
            is_right = 0 if (hidx == 0 and num_hands == 2) else 1
            
            # load hamba data assets
            frame_data = []
            for cidx in range(NUM_CAMS):
                if hand_data[cidx] is not None and f'{fidx}_{hidx}' in hand_data[cidx].keys():
                    frame_data.append(hand_data[cidx][f'{fidx}_{hidx}'])
                else:
                    frame_data.append(None)

            obj_pts_f = obj_all[fidx] if obj_all is not None else None
            verts, params = register(frame_data, c2ws, is_right, prev_params[hidx], ref_cidx[hidx], fidx, mp_out, obj_pts=obj_pts_f)

            vertices_world_reg.append(verts) # not necessary
            prev_params[hidx] = params

            side = 'right' if is_right else 'left'
            out_dict[fidx][side] = verts #vertices_world_reg[hidx]
            out_dict_params[fidx][side] = params #vertices_world_reg[hidx]

        if True:

            output_image_ = []

            for cam_idx in range(NUM_CAMS):

                for hidx in range(num_hands):
                    #is_right = 0 if hidx == 0 else 1
                    is_right = 0 if (hidx == 0 and num_hands == 2) else 1
                    vertices_world_reg_ = np.asarray(vertices_world_reg[hidx]).reshape(-1, 3)
                    if num_hands == 1: hidx = 1

                    T_c2w = c2ws[cam_idx]
                    T_w2c = np.linalg.inv(T_c2w)

                    # Convert vertices_world to homogeneous coordinates
                    vertices_world_hom = np.hstack([vertices_world_reg_, np.ones((vertices_world_reg_.shape[0], 1))])  # (N, 4)

                    # Apply inverse transform
                    vertices_hom = (T_w2c @ vertices_world_hom.T).T  # (N, 4)

                    # Optionally extract 3D camera coordinates
                    vertices_cam = vertices_hom[:, :3]

                    rgb_render, alpha = render(vertices_cam, is_right, cam_idx=cam_idx)
                    
                    if hidx == 0 or num_hands == 1:
                        bg = cv2.imread(f"{phystwin_base_path}/{case_name}/color/{cam_idx}/{fidx}.png")  # BGR format
                        if bg is None:
                            break
                        bg = bg.astype(np.float32) / 255.0 

                    bg = composite = rgb_render * alpha + bg * (1 - alpha)

                    '''
                    points_2d = hamba_3d_to_2d(vertices_cam)

                    output_image = image.copy()

                    color = (0, 0, 255)

                    # Loop over all points and set the pixel
                    points = np.round(points_2d).astype(np.int32)

                    for x, y in points:

                        # Make sure the point is inside image bounds
                        if 0 <= x < image.shape[1] and 0 <= y < image.shape[0]:
                            output_image[y, x] = color  # Note: image[y, x], not [x, y]

                    # Step 4: Save the modified image
                    output_image_.append(output_image)
                    '''
                output_image_.append((composite * 255).astype(np.uint8))

            os.makedirs(f"../debug/_hand_reg_mp/{log}/{case_name}/", exist_ok=True)
            cv2.imwrite(f"../debug/_hand_reg_mp/{log}/{case_name}/{fidx:03d}_{hidx}.png", cv2.vconcat(output_image_))

        # Save only completed frames. One atomic file keeps vertices/parameters consistent.
        checkpoint_tmp = checkpoint_path + '.tmp'
        with open(checkpoint_tmp, 'wb') as f:
            pickle.dump({'config':checkpoint_config, 'last_frame':fidx,
                         'out':out_dict, 'params':out_dict_params, 'rng':rng_state()}, f)
        os.replace(checkpoint_tmp, checkpoint_path)
        # Audit resume from a completed middle frame without touching live checkpoint.
        if fidx == (start_frame + loop_end)//2:
            import shutil
            shutil.copy2(checkpoint_path, checkpoint_path+'.midpoint')
        if args.stop_after_frames and fidx-loop_start+1 >= args.stop_after_frames:
            raise SystemExit(75)

    timer.end("process_hands-optimize")
    with open(out_path, 'wb') as f:
        pickle.dump(out_dict, f)

    with open(out_path_params, 'wb') as f:
        pickle.dump(out_dict_params, f)

    timer.end("process_hands")
    timer.write_to_file(f"{phystwin_base_path}/{case_name}/{log}/timelog.txt")

if __name__ == '__main__':

    if case_name == 'all':
        for curr_case in os.listdir(hamba_base_path):
            process_hands(curr_case)

    else:
        process_hands(case_name)

