# FIlter the tracking based on the object and controller mask, filter the track based on the neighbour motion
# Get the nearest controller points that are valid across all frames

import numpy as np
import open3d as o3d
from tqdm import tqdm
import os
import glob
import pickle
import matplotlib.pyplot as plt
from argparse import ArgumentParser

parser = ArgumentParser()
parser.add_argument(
    "--base_path",
    type=str,
    required=True,
)
parser.add_argument("--case_name", type=str, required=True)
parser.add_argument("--no_viz", action="store_true", default=False,
                    help="skip the per-frame final_data-style render (verification video; "
                         "~15-20min on a 1-minute clip). track_process_data.pkl is unaffected.")
args = parser.parse_args()

base_path = args.base_path
case_name = args.case_name
NO_VIZ = args.no_viz


def exist_dir(dir):
    if not os.path.exists(dir):
        os.makedirs(dir)


def getSphereMesh(center, radius=0.1, color=[0, 0, 0]):
    sphere = o3d.geometry.TriangleMesh.create_sphere(radius=radius).translate(center)
    sphere.paint_uniform_color(color)
    return sphere


# Based on the valid mask, filter out the bad tracking data
def filter_track(track_path, pcd_path, mask_path, frame_num, num_cam):
    with open(f"{mask_path}/processed_masks.pkl", "rb") as f:
        processed_masks = pickle.load(f)

    # Filter out the points not valid in the first frame
    object_points = []
    object_colors = []
    object_visibilities = []
    controller_points = []
    controller_colors = []
    controller_visibilities = []
    for i in range(num_cam):
        current_track_data = np.load(f"{track_path}/{i}.npz")
        # Filter out the track data
        tracks = current_track_data["tracks"]
        tracks = np.round(tracks).astype(int)
        visibility = current_track_data["visibility"]
        assert tracks.shape[0] == frame_num
        num_points = np.shape(tracks)[1]
        # clamp track coords to valid image bounds: rounding can push an edge track to
        # H or W (e.g. y=480 on a 480-row image), which indexes out of the pcd/mask
        # arrays below. clip to [0,H-1]/[0,W-1] (edge tracks stay on the border pixel).
        _H, _W = processed_masks[0][i]["object"].shape[:2]
        tracks[:, :, 0] = np.clip(tracks[:, :, 0], 0, _H - 1)
        tracks[:, :, 1] = np.clip(tracks[:, :, 1], 0, _W - 1)

        # Locate the track points in the object mask of the first frame
        object_mask = processed_masks[0][i]["object"]
        track_object_idx = np.zeros((num_points), dtype=int)
        for j in range(num_points):
            if visibility[0, j] == 1:
                track_object_idx[j] = object_mask[tracks[0, j, 0], tracks[0, j, 1]]
        # Locate the controller points in the controller mask of the first frame
        controller_mask = processed_masks[0][i]["controller"]
        track_controller_idx = np.zeros((num_points), dtype=int)
        for j in range(num_points):
            if visibility[0, j] == 1:
                track_controller_idx[j] = controller_mask[
                    tracks[0, j, 0], tracks[0, j, 1]
                ]

        # Filter out bad tracking in other frames
        for frame_idx in range(1, frame_num):
            # Filter based on object_mask
            object_mask = processed_masks[frame_idx][i]["object"]
            for j in range(num_points):
                try:
                    if track_object_idx[j] == 1 and visibility[frame_idx, j] == 1:
                        if not object_mask[
                            tracks[frame_idx, j, 0], tracks[frame_idx, j, 1]
                        ]:
                            visibility[frame_idx, j] = 0
                except:
                    # Sometimes the track coordinate is out of image
                    visibility[frame_idx, j] = 0
            # Filter based on controller_mask
            controller_mask = processed_masks[frame_idx][i]["controller"]
            for j in range(num_points):
                if track_controller_idx[j] == 1 and visibility[frame_idx, j] == 1:
                    if not controller_mask[
                        tracks[frame_idx, j, 0], tracks[frame_idx, j, 1]
                    ]:
                        visibility[frame_idx, j] = 0

        # Get the track point cloud
        track_points = np.zeros((frame_num, num_points, 3))
        track_colors = np.zeros((frame_num, num_points, 3))
        for frame_idx in range(frame_num):
            data = np.load(f"{pcd_path}/{frame_idx}.npz")
            points = data["points"]
            colors = data["colors"]

            track_points[frame_idx][np.where(visibility[frame_idx])] = points[i][
                tracks[frame_idx, np.where(visibility[frame_idx])[0], 0],
                tracks[frame_idx, np.where(visibility[frame_idx])[0], 1],
            ]
            track_colors[frame_idx][np.where(visibility[frame_idx])] = colors[i][
                tracks[frame_idx, np.where(visibility[frame_idx])[0], 0],
                tracks[frame_idx, np.where(visibility[frame_idx])[0], 1],
            ]

        object_points.append(track_points[:, np.where(track_object_idx)[0], :])
        object_colors.append(track_colors[:, np.where(track_object_idx)[0], :])
        object_visibilities.append(visibility[:, np.where(track_object_idx)[0]])
        controller_points.append(track_points[:, np.where(track_controller_idx)[0], :])
        controller_colors.append(track_colors[:, np.where(track_controller_idx)[0], :])
        controller_visibilities.append(visibility[:, np.where(track_controller_idx)[0]])

    object_points = np.concatenate(object_points, axis=1)
    object_colors = np.concatenate(object_colors, axis=1)
    object_visibilities = np.concatenate(object_visibilities, axis=1)
    controller_points = np.concatenate(controller_points, axis=1)
    controller_colors = np.concatenate(controller_colors, axis=1)
    controller_visibilities = np.concatenate(controller_visibilities, axis=1)

    track_data = {}
    track_data["object_points"] = object_points
    track_data["object_colors"] = object_colors
    track_data["object_visibilities"] = object_visibilities
    track_data["controller_points"] = controller_points
    track_data["controller_colors"] = controller_colors
    track_data["controller_visibilities"] = controller_visibilities

    return track_data


def filter_motion(track_data, neighbor_dist=0.01):
    from motion_contract import frozen_motion_filter, SCHEMA
    track_data["object_motions_valid"] = frozen_motion_filter(
        track_data["object_points"], track_data["object_visibilities"], neighbor_dist)
    vis = track_data["controller_visibilities"].astype(bool)
    eligible = vis.all(axis=0)
    mv = frozen_motion_filter(track_data["controller_points"], vis, neighbor_dist, eligible)
    track_data["controller_motions_valid"] = mv
    track_data["controller_mask"] = eligible & mv[:-1].all(axis=0)
    track_data["motion_schema"] = dict(SCHEMA)
    return track_data


def get_final_track_data(track_data, controller_threhsold=0.01):
    object_points = track_data["object_points"]
    object_colors = track_data["object_colors"]
    object_visibilities = track_data["object_visibilities"]
    object_motions_valid = track_data["object_motions_valid"]
    controller_points = track_data["controller_points"]
    mask = track_data["controller_mask"]

    # No controller (hand) points (object-only track, no physics) -> leave empty.
    if controller_points.shape[1] == 0 or int(np.sum(mask)) < 30:
        track_data.pop("controller_points")
        track_data.pop("controller_colors")
        track_data.pop("controller_visibilities")
        track_data["controller_points"] = np.zeros((object_points.shape[0], 0, 3))
        track_data["controller_motions_valid"] = np.zeros((object_points.shape[0], 0), dtype=bool)
        return track_data

    new_controller_points = controller_points[:, np.where(mask)[0], :]
    assert len(new_controller_points[0]) >= 30
    # Do farthest point sampling on the valid controller points to select the final controller points
    valid_indices = np.arange(len(new_controller_points[0]))
    points_map = {}
    sample_points = []
    for i in valid_indices:
        points_map[tuple(new_controller_points[0, i])] = i
        sample_points.append(new_controller_points[0, i])
    sample_points = np.array(sample_points)
    sample_pcd = o3d.geometry.PointCloud()
    sample_pcd.points = o3d.utility.Vector3dVector(sample_points)
    fps_pcd = sample_pcd.farthest_point_down_sample(30)
    final_indices = []
    for point in fps_pcd.points:
        final_indices.append(points_map[tuple(point)])

    print(f"Controller Point Number: {len(final_indices)}")

    # Get the nearest controller points and their colors
    nearest_controller_points = new_controller_points[:, final_indices]

    # object_pcd = o3d.geometry.PointCloud()
    # object_pcd.points = o3d.utility.Vector3dVector(valid_object_points)
    # object_pcd.colors = o3d.utility.Vector3dVector(
    #     object_colors[0][np.where(object_motions_valid[0])]
    # )
    # controller_meshes = []
    # for j in range(nearest_controller_points.shape[1]):
    #     origin = nearest_controller_points[0, j]
    #     origin_color = [1, 0, 0]
    #     controller_meshes.append(
    #         getSphereMesh(origin, color=origin_color, radius=0.005)
    #     )
    # o3d.visualization.draw_geometries([object_pcd])
    # o3d.visualization.draw_geometries([object_pcd] + controller_meshes)

    track_data.pop("controller_points")
    track_data.pop("controller_colors")
    track_data.pop("controller_visibilities")
    track_data["controller_points"] = nearest_controller_points
    track_data["controller_motions_valid"] = track_data["controller_motions_valid"][:, np.flatnonzero(mask)[final_indices]]

    return track_data


def visualize_track(track_data):
    object_points = track_data["object_points"]
    object_colors = track_data["object_colors"]
    object_visibilities = track_data["object_visibilities"]
    object_motions_valid = track_data["object_motions_valid"]
    controller_points = track_data["controller_points"]

    frame_num = object_points.shape[0]

    vis = o3d.visualization.Visualizer()
    vis.create_window(visible=False)
    controller_meshes = []
    prev_center = []

    y_min, y_max = np.min(object_points[0, :, 1]), np.max(object_points[0, :, 1])
    y_normalized = (object_points[0, :, 1] - y_min) / (y_max - y_min)
    rainbow_colors = plt.cm.rainbow(y_normalized)[:, :3]

    for i in range(frame_num):
        object_pcd = o3d.geometry.PointCloud()
        object_pcd.points = o3d.utility.Vector3dVector(
            object_points[i, np.where(object_motions_valid[i])[0], :]
        )
        # object_pcd.colors = o3d.utility.Vector3dVector(
        #     object_colors[i, np.where(object_motions_valid[i])[0], :]
        # )
        object_pcd.colors = o3d.utility.Vector3dVector(
            rainbow_colors[np.where(object_motions_valid[i])[0]]
        )

        if i == 0:
            render_object_pcd = object_pcd
            vis.add_geometry(render_object_pcd)
            # Use sphere mesh for each controller point
            for j in range(controller_points.shape[1]):
                origin = controller_points[i, j]
                origin_color = [1, 0, 0]
                controller_meshes.append(
                    getSphereMesh(origin, color=origin_color, radius=0.01)
                )
                vis.add_geometry(controller_meshes[-1])
                prev_center.append(origin)
            # Adjust the viewpoint
            view_control = vis.get_view_control()
            view_control.set_front([1, 0, -2])
            view_control.set_up([0, 0, -1])
            view_control.set_zoom(1)
        else:
            render_object_pcd.points = o3d.utility.Vector3dVector(object_pcd.points)
            render_object_pcd.colors = o3d.utility.Vector3dVector(object_pcd.colors)
            vis.update_geometry(render_object_pcd)
            for j in range(controller_points.shape[1]):
                origin = controller_points[i, j]
                controller_meshes[j].translate(origin - prev_center[j])
                vis.update_geometry(controller_meshes[j])
                prev_center[j] = origin
            vis.poll_events()
            vis.update_renderer()


if __name__ == "__main__":
    pcd_path = f"{base_path}/{case_name}/pcd"
    mask_path = f"{base_path}/{case_name}/mask"
    track_path = f"{base_path}/{case_name}/cotracker"

    num_cam = len(glob.glob(f"{mask_path}/mask_info_*.json"))
    frame_num = len(glob.glob(f"{pcd_path}/*.npz"))

    # Filter the track data using the semantic mask of object and controller
    track_data = filter_track(track_path, pcd_path, mask_path, frame_num, num_cam)
    # Filter motion
    track_data = filter_motion(track_data)
    # # Save the filtered track data
    # with open(f"test2.pkl", "wb") as f:
    #     pickle.dump(track_data, f)

    # with open(f"test2.pkl", "rb") as f:
    #     track_data = pickle.load(f)

    track_data = get_final_track_data(track_data)

    from motion_contract import validate_motion
    validate_motion(track_data, "object")
    from export_provenance import atomic_pickle
    atomic_pickle(f"{base_path}/{case_name}/track_process_data.pkl", track_data)

    if not NO_VIZ:
        visualize_track(track_data)
