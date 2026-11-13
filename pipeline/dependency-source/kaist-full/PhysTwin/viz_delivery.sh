#!/bin/bash
# Regenerate/convert the delivery verification videos for ONE case, all to H.264, using
# the EXISTING viz scripts (no new heavy renderer). Usage: bash viz_delivery.sh <case_dir>
set -e
D="$1"
CN=$(basename "$D")
BP="${KP_DATA_BASE:?KP_DATA_BASE not set (use bin/kp)}"
PT="${KP_PY_PHYSTWIN:?KP_PY_PHYSTWIN not set (use bin/kp)}"
enc() { ffmpeg -y -i "$1" -c:v libx264 -pix_fmt yuv420p "$2" >/dev/null 2>&1 && rm -f "$1"; }

echo "[$CN] object 2D tracking -> H.264"
for c in 0 1 2 3; do
  [ -f "$D/cotracker/${c}_30fps.mp4" ] && enc "$D/cotracker/${c}_30fps.mp4" "$D/cotracker/${c}_tracking.mp4"
done

echo "[$CN] segmentation (tiled) -> mask/segmentation.mp4"
$PT data_process/viz_sam_masks.py --case "$D" >/dev/null 2>&1 || true
[ -f "$D/viz_sam_masks.mp4" ] && enc "$D/viz_sam_masks.mp4" "$D/mask/segmentation.mp4"

echo "[$CN] hand 2D tracking -> H.264"
$PT data_process/viz_cotracker.py --base_path "$BP" --case_name "$CN" --track_dir cotracker/hand >/dev/null 2>&1 || true
for c in 0 1 2 3; do
  [ -f "$D/cotracker/hand/${c}_vischeck.mp4" ] && enc "$D/cotracker/hand/${c}_vischeck.mp4" "$D/cotracker/hand/${c}_tracking.mp4"
done

echo "[$CN] object 3D (TRELLIS) -> shape/object_recon.mp4"
[ -f "$D/shape/visualization.mp4" ] && ffmpeg -y -i "$D/shape/visualization.mp4" -c:v libx264 -pix_fmt yuv420p "$D/shape/object_recon.mp4" >/dev/null 2>&1

echo "[$CN] object+hand 3D -> obj_hand_recon.mp4"
if [ ! -f "$D/obj_hand_recon.mp4" ]; then
  $PT data_process/render_obj_hand_recon.py --case "$D" --object_id 0 --hand_id 1 --up_z 1 --out "$D/_recon_tmp.mp4" >/dev/null 2>&1
  enc "$D/_recon_tmp.mp4" "$D/obj_hand_recon.mp4"
fi
echo "[$CN] viz done"
