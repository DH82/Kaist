from pathlib import Path
import torch
import argparse
import os
import cv2
import numpy as np

import os
#os.environ["CUDA_VISIBLE_DEVICES"]="0"

from hamba.configs import CACHE_DIR_HAMBA
from hamba.models import HAMBA, download_models, load_hamba, DEFAULT_CHECKPOINT
from hamba.utils import recursive_to
from hamba.datasets.vitdet_dataset import ViTDetDataset, DEFAULT_MEAN, DEFAULT_STD
from hamba.utils.renderer import Renderer, cam_crop_to_full

LIGHT_BLUE=(0, 0.278, 0.671)

from vitpose_model import ViTPoseModel

import json
from typing import Dict, Optional

import shutil

def main():
    parser = argparse.ArgumentParser(description='hamba demo code')
    parser.add_argument('--img_folder', type=str, default='/workspace/deform_hoi/PhysTwin/data_ours/different_types/', help='Folder with input images')
    parser.add_argument('--checkpoint', type=str, default="ckpts/hamba/checkpoints/hamba.ckpt", help='Path to pretrained model checkpoint')
    parser.add_argument('--out_folder', type=str, default='./hand_recon_out', help='Output folder to save rendered results')
    parser.add_argument('--side_view', dest='side_view', action='store_true', default=False, help='If set, render side view also')
    parser.add_argument('--full_frame', dest='full_frame', action='store_true', default=True, help='If set, render all people together also')
    parser.add_argument('--save_mesh', dest='save_mesh', action='store_true', default=True, help='If set, save meshes to disk also')
    parser.add_argument('--batch_size', type=int, default=1, help='Batch size for inference/fitting')
    parser.add_argument('--rescale_factor', type=float, default=2.0, help='Factor for padding the bbox')
    parser.add_argument('--body_detector', type=str, default='vitdet', choices=['vitdet', 'regnety'], help='Using regnety improves runtime and reduces memory')
    parser.add_argument('--file_type', nargs='+', default=['*.jpg', '*.png'], help='List of file extensions to consider')
    parser.add_argument('--single_person', action='store_true',
                        help="[local] keep only the highest-scoring person box. vitdet "
                             "sometimes splits one person into two overlapping boxes when the "
                             "arm enters from a frame edge; ViTPose then yields a hand pair per "
                             "box, and the >=3 detections land in the else-branch below that "
                             "writes None over hand slots 0/1 -- the MANO fit then dies on "
                             "ref_cidx[1] (ep_20260826_084827 / _084841). Only safe when the "
                             "capture really has one person, so it is opt-in.")
    args = parser.parse_args()
    args.full_frame = True

    # Completed camera results are reusable; avoid rebuilding models on resume.
    pending = []
    for seq in sorted(Path(args.img_folder).iterdir()):
        color = seq / 'color'
        if color.is_dir():
            for cam in color.iterdir():
                if cam.is_dir() and cam.name.isdigit() and not (Path(args.out_folder)/seq.name/cam.name/'results.pt').is_file():
                    pending.append((seq.name,cam.name))
    if not pending:
        print('All camera results already exist; validating via stepper review.')
        return

    # Download and load checkpoints
    model, model_cfg = load_hamba(args.checkpoint)

    # Setup HAMBA model
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
    model = model.to(device)
    model.eval()

    # Load detector
    from hamba.utils.utils_detectron2 import DefaultPredictor_Lazy
    if args.body_detector == 'vitdet':
        from detectron2.config import LazyConfig
        import hamba
        cfg_path = Path(hamba.__file__).parent/'configs'/'cascade_mask_rcnn_vitdet_h_75ep.py'
        detectron2_cfg = LazyConfig.load(str(cfg_path))
        detectron2_cfg.train.init_checkpoint = "https://dl.fbaipublicfiles.com/detectron2/ViTDet/COCO/cascade_mask_rcnn_vitdet_h/f328730692/model_final_f05665.pkl"
        for i in range(3):
            detectron2_cfg.model.roi_heads.box_predictors[i].test_score_thresh = 0.1
        detector = DefaultPredictor_Lazy(detectron2_cfg)
    elif args.body_detector == 'regnety':
        from detectron2 import model_zoo
        from detectron2.config import get_cfg
        detectron2_cfg = model_zoo.get_config('new_baselines/mask_rcnn_regnety_4gf_dds_FPN_400ep_LSJ.py', trained=True)
        detectron2_cfg.model.roi_heads.box_predictor.test_score_thresh = 0.5
        detectron2_cfg.model.roi_heads.box_predictor.test_nms_thresh   = 0.4
        detector       = DefaultPredictor_Lazy(detectron2_cfg)

    # keypoint detector
    cpm = ViTPoseModel(device)

    # Setup the renderer
    renderer = Renderer(model_cfg, faces=model.mano.faces)

    # Make output directory if it does not exist
    os.makedirs(args.out_folder, exist_ok=True)
    folder_list = os.listdir(args.img_folder)
    folder_list.sort()

    #folder_list = folder_list[:len(folder_list)//4]
    #folder_list = folder_list[len(folder_list)//4:len(folder_list)//2]
    #folder_list = folder_list[len(folder_list)//2:3*len(folder_list)//4]
    #folder_list = folder_list[3*len(folder_list)//4:]

    for seq in folder_list:

        ## WARNING!
        #if seq != 'double_stretch_sloth': continue
        #if seq != 'double_lift_cloth_3': continue
        #if seq != 'double_lift_cloth_1': continue
        #if seq != 'double_stretch_zebra': continue
        #if seq != 'double_lift_zebra': continue
        #if seq != 'weird_package': continue
        #if seq != 'rope_double_hand': continue
        #if seq != 'single_lift_dinosor': continue
        #if seq != 'single_push_rope': continue
        # (removed hardcoded '250703' seq filter — img_folder already scopes the case)

        in_dir = os.path.join(args.img_folder, seq, 'color')
        print(f'Processing seq \"{seq}\"...')

        # WARNING!
        # iterate the actual numeric cam dirs (PhysTwin=3 cams, DynamicDenseHOI=4)
        view_dirs = sorted(int(d) for d in os.listdir(in_dir) if d.isdigit())
        for view_idx in view_dirs:
            print(f'  - Viewpoint \"{view_idx}\"...')
            curr_out_folder = os.path.join(args.out_folder, seq, f'{view_idx}')
            os.makedirs(curr_out_folder, exist_ok=True)

            if os.path.exists(os.path.join(curr_out_folder, 'results.pt')):
                print(f'continue {seq}-{view_idx}...')
                continue

            out_dict = {}

            # Get all demo images ends with .jpg or .png
            img_paths = [img for end in args.file_type for img in Path(os.path.join(in_dir, f'{view_idx}')).glob(end)]
            img_paths = sorted(img_paths)

            # Iterate over all images in folder
            for img_path in img_paths:
                #if '115' not in str(img_path):
                #    continue
                img_fn = img_path.stem
                img_cv2 = cv2.imread(str(img_path))
                print("input img_path: ", img_path)

                # Detect humans in image
                det_out = detector(img_cv2)
                img = img_cv2.copy()[:, :, ::-1]

                det_instances = det_out['instances']
                valid_idx = (det_instances.pred_classes==0) & (det_instances.scores > 0.5)
                pred_bboxes=det_instances.pred_boxes.tensor[valid_idx].cpu().numpy()
                pred_scores=det_instances.scores[valid_idx].cpu().numpy()

                # [local] see --single_person: collapse duplicate person boxes to the best one
                if args.single_person and len(pred_bboxes) > 1:
                    keep = int(np.argmax(pred_scores))
                    print(f"[single_person] {len(pred_bboxes)} person boxes -> keeping "
                          f"score={pred_scores[keep]:.3f}")
                    pred_bboxes = pred_bboxes[keep:keep + 1]
                    pred_scores = pred_scores[keep:keep + 1]

                # Detect human keypoints for each person
                vitposes_out = cpm.predict_pose(
                    img,
                    [np.concatenate([pred_bboxes, pred_scores[:, None]], axis=1)],
                )

                bboxes = []
                is_right = []
                keypoints_2d_list = []

                # Use hands based on hand keypoint detections
                for vitposes in vitposes_out:
                    left_hand_keyp = vitposes['keypoints'][-42:-21] # 3D keypoints
                    right_hand_keyp = vitposes['keypoints'][-21:]

                    # Rejecting not confident detections
                    keyp = left_hand_keyp
                    valid = keyp[:,2] > 0.1
                    if sum(valid) > 3:
                        bbox = [keyp[valid,0].min(), keyp[valid,1].min(), keyp[valid,0].max(), keyp[valid,1].max()]
                        bboxes.append(bbox)
                        is_right.append(0)
                    keypoints_2d_list.append(left_hand_keyp) 

                    keyp = right_hand_keyp
                    valid = keyp[:,2] > 0.1
                    if sum(valid) > 3:
                        bbox = [keyp[valid,0].min(), keyp[valid,1].min(), keyp[valid,0].max(), keyp[valid,1].max()]
                        bboxes.append(bbox)
                        is_right.append(1)
                    keypoints_2d_list.append(right_hand_keyp)

                    keypoints_2d_to_save = vitposes['keypoints']

                if len(bboxes) == 0:
                    img_fn, _ = os.path.splitext(os.path.basename(img_path))
                    all_mesh_path = os.path.join(curr_out_folder, f'{img_fn}_all.jpg')
                    cv2.imwrite(all_mesh_path, img_cv2)
                    continue

                boxes = np.stack(bboxes)
                right = np.stack(is_right)
                keypoints_2d_arr = np.stack(keypoints_2d_list)

                # Run reconstruction on all detected hands
                dataset = ViTDetDataset(model_cfg, img_cv2, boxes, right, rescale_factor=args.rescale_factor, keypoints_2d_arr=keypoints_2d_arr)
                dataloader = torch.utils.data.DataLoader(dataset, batch_size=1, shuffle=False, num_workers=0)

                all_verts = []
                all_cam_t = []
                all_cam_t_hamba = []
                all_right = []
                
                for batch in dataloader:
                    if -1 in batch["is_valid"]:
                        print("no hand detection: ", img_path)
                        img_fn, _ = os.path.splitext(os.path.basename(img_path))
                        all_mesh_path = os.path.join(curr_out_folder, f'{img_fn}_all.jpg')
                        cv2.imwrite(all_mesh_path, img_cv2)
                        print("cp src: ", all_mesh_path)
                        continue

                    batch = recursive_to(batch, device)

                    with torch.no_grad():
                        out = model(batch)

                    # Parse the model outputs
                    multiplier = (2*batch['right']-1)
                    pred_cam = out['pred_cam']
                    pred_cam[:,1] = multiplier*pred_cam[:,1]
                    box_center = batch["box_center"].float()
                    box_size = batch["box_size"].float()
                    img_size = batch["img_size"].float()
                    multiplier = (2*batch['right']-1)
                    scaled_focal_length = model_cfg.EXTRA.FOCAL_LENGTH / model_cfg.MODEL.IMAGE_SIZE * img_size.max()

                    pred_cam_t_full_hamba = cam_crop_to_full(pred_cam, box_center, box_size, img_size, scaled_focal_length).detach().cpu().numpy()

                    print('WARNING!')
                    scaled_focal_length = 427.
                    pred_cam_t_full = cam_crop_to_full(pred_cam, box_center, box_size, img_size, scaled_focal_length).detach().cpu().numpy()

                    # Render the result
                    batch_size = batch['img'].shape[0]
                    for n in range(batch_size):
                        # Get filename from path img_path
                        img_fn, _ = os.path.splitext(os.path.basename(img_path))
                        detection_id = int(batch['personid'][n])
                        person_id = int(batch['right'][n].item())
                        white_img = (torch.ones_like(batch['img'][n]).cpu() - DEFAULT_MEAN[:,None,None]/255) / (DEFAULT_STD[:,None,None]/255)
                        input_patch = batch['img'][n].cpu() * (DEFAULT_STD[:,None,None]/255) + (DEFAULT_MEAN[:,None,None]/255)
                        input_patch = input_patch.permute(1,2,0).numpy()

                        regression_img = renderer(out['pred_vertices'][n].detach().cpu().numpy(),
                                                out['pred_cam_t'][n].detach().cpu().numpy(),
                                                batch['img'][n],
                                                mesh_base_color=LIGHT_BLUE,
                                                scene_bg_color=(1, 1, 1),
                                                )

                        if args.side_view:
                            side_img = renderer(out['pred_vertices'][n].detach().cpu().numpy(),
                                                    out['pred_cam_t'][n].detach().cpu().numpy(),
                                                    white_img,
                                                    mesh_base_color=LIGHT_BLUE,
                                                    scene_bg_color=(1, 1, 1),
                                                    side_view=True)
                            final_img = np.concatenate([input_patch, regression_img, side_img], axis=1)
                        else:
                            final_img = np.concatenate([input_patch, regression_img], axis=1)

                        cv2.imwrite(os.path.join(curr_out_folder, f'{img_fn}_{person_id}.png'), 255*final_img[:, :, ::-1])

                        # Add all verts and cams to list
                        verts = out['pred_vertices'][n].detach().cpu().numpy()
                        is_right = batch['right'][n].cpu().numpy()
                        verts[:,0] = (2*is_right-1)*verts[:,0]
                        cam_t = pred_cam_t_full[n]
                        all_verts.append(verts)
                        all_cam_t.append(cam_t)
                        all_cam_t_hamba.append(pred_cam_t_full_hamba[n])
                        all_right.append(is_right)

                        # Save all meshes to disk
                        if args.save_mesh:
                            camera_translation = cam_t.copy()
                            
                            # WARNING! Discard z prediction?
                            #camera_translation[..., -1] = 0.

                            tmesh = renderer.vertices_to_trimesh(verts, camera_translation, LIGHT_BLUE, is_right=is_right)
                            tmesh.export(os.path.join(curr_out_folder, f'{img_fn}_{person_id}.obj'))
                        
                        # Save the metainfo
                        # Serialize anatomical side, not detection order. Batch size is one.
                        record = dict(out)
                        record['is_right'] = person_id
                        record['detection_id'] = detection_id
                        record['pred_cam_t_org'] = cam_t.copy()
                        record['pred_cam_t_hamba'] = pred_cam_t_full_hamba[n].copy()
                        hand_key = f'{img_fn}_{person_id}'
                        # Multiple people/detections for one side are ambiguous.
                        out_dict[hand_key] = None if hand_key in out_dict else record

                        # need to save camera_translation here

                # Render front view
                if args.full_frame and len(all_verts) > 0:
                    misc_args = dict(
                        mesh_base_color=LIGHT_BLUE,
                        scene_bg_color=(0, 0, 0),
                        focal_length=scaled_focal_length,
                    )
                    cam_view = renderer.render_rgba_multiple(all_verts, cam_t=all_cam_t, render_res=img_size[n], is_right=all_right, **misc_args)

                    # Overlay image
                    input_img = img_cv2.astype(np.float32)[:,:,::-1]/255.0
                    
                    # below for egl
                    # input_img = np.concatenate([input_img, np.ones_like(input_img[:,:,:1])], axis=2) # Add alpha channel
                    # input_img_overlay = input_img[:,:,:3] * (1-cam_view[:,:,3:]) + cam_view[:,:,:3] * cam_view[:,:,3:]
                    
                    valid_mask = (cam_view[:, :, -1] > 0)[:, :, np.newaxis]
                    input_img_overlay = input_img[:,:,:3] * (1 - valid_mask) + cam_view[:,:,:3] * valid_mask
                    final_img = 255*input_img_overlay[:, :, ::-1]

                    all_mesh_path = os.path.join(curr_out_folder, f'{img_fn}_all.jpg')
                    cv2.imwrite(all_mesh_path, final_img)
                    print("all_mesh_path: ", all_mesh_path)

                # [local] dataset is rebuilt every frame and owns a MediaPipe graph; release
                # it here or its GL contexts/threads accumulate in the NVIDIA driver until
                # Xorg segfaults. Safe at this point: mp_hand is only touched in __getitem__.
                dataset.close()

            for image_path in img_paths:
                for side in (0, 1):
                    out_dict.setdefault(f'{image_path.stem}_{side}', None)

            # Save data
            torch.save(out_dict, os.path.join(curr_out_folder, 'results.pt'))

if __name__ == '__main__':
    main()
