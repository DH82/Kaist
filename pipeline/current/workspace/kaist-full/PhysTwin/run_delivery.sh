#!/bin/bash
# Full per-episode delivery: downstream (CoTracking + 3D recon + finalize) -> verification
# viz -> clean to the agreed delivery format (raw archived, non-delivery intermediates
# dropped) -> zip. object_name/interaction come from data_process/episodes.py.
#   Usage: bash run_delivery.sh <ep_dir_name> [--keep]
#
# --keep : do not delete anything that later verification needs. The 2026-08-26 captures are
#   short (137-190 frames) so the space cost is small, and the spring-graph / IoU analysis
#   needs shape/matching/final_mesh.glb + shape/object.ply, which stage 3/4 normally removes.
#   The delivery ZIP is unaffected: the kept extras are excluded from it, so the archive that
#   ships stays byte-for-byte the agreed format. Raw cam_* is MOVED to raw_archive/ either
#   way (a move, not a delete), so it survives regardless.
set -u
EP="$1"
KEEP=0
[ "${2:-}" = "--keep" ] && KEEP=1
BP="${KP_DATA_BASE:?KP_DATA_BASE not set (use bin/kp)}"
D="$BP/$EP"
PT="${KP_PY_PHYSTWIN:?KP_PY_PHYSTWIN not set (use bin/kp)} -u"
enc(){ ffmpeg -nostdin -y -i "$1" -c:v mpeg4 -q:v 3 -pix_fmt yuv420p "$2" >/dev/null 2>&1 && rm -f "$1"; }  # mpeg4(mp4v): H.264 미사용, 어디서든 디코딩

info=$($PT -c \
  "from data_process.episodes import EPISODES as E; e=next(x for x in E if x['ep']=='$EP'); print(e['name']+'|'+e['interaction'])")
OBJ="${info%%|*}"; INT="${info#*|}"
echo "EP=$EP  OBJ=$OBJ"

echo "########## 1/4 downstream (cot + recon + finalize) ##########"
if [ -f "$D/final_data.pkl" ]; then
  echo "  final_data.pkl 존재 → downstream 스킵 (이미 처리됨)"
else
  $PT data_process/rerun_from_seg.py --case "$D" --object_name "$OBJ" --interaction "$INT" \
      $([ $KEEP -eq 1 ] && echo --keep_verification)
fi

echo "########## 2/4 verification viz ##########"
$PT data_process/viz_sam_masks.py --case "$D" >/dev/null 2>&1
[ -f "$D/viz_sam_masks.mp4" ] && enc "$D/viz_sam_masks.mp4" "$D/mask/segmentation.mp4"
$PT data_process/viz_cotracker.py --base_path "$BP" --case_name "$EP" --track_dir cotracker >/dev/null 2>&1
for c in 0 1 2 3; do [ -f "$D/cotracker/${c}_vischeck.mp4" ] && enc "$D/cotracker/${c}_vischeck.mp4" "$D/cotracker/${c}_tracking.mp4"; done
$PT data_process/viz_cotracker.py --base_path "$BP" --case_name "$EP" --track_dir cotracker/hand >/dev/null 2>&1
for c in 0 1 2 3; do [ -f "$D/cotracker/hand/${c}_vischeck.mp4" ] && enc "$D/cotracker/hand/${c}_vischeck.mp4" "$D/cotracker/hand/${c}_tracking.mp4"; done
[ -f "$D/shape/visualization.mp4" ] && ffmpeg -y -i "$D/shape/visualization.mp4" -c:v mpeg4 -q:v 3 -pix_fmt yuv420p "$D/shape/object_recon.mp4" >/dev/null 2>&1
[ -f "$D/obj_hand_recon.mp4" ] || { $PT data_process/render_obj_hand_recon.py --case "$D" --object_id 0 --hand_id 1 --up_z 1 --out "$D/_recon_tmp.mp4" >/dev/null 2>&1; enc "$D/_recon_tmp.mp4" "$D/obj_hand_recon.mp4"; }

echo "########## 3/4 clean to delivery format ##########"
RA="$BP/raw_archive/$EP"; mkdir -p "$RA"; mv "$D"/cam_* "$RA"/ 2>/dev/null
if [ $KEEP -eq 1 ]; then
  echo "  --keep: shape/matching, shape/object.ply, cotracker check mp4s, hand_track_data.pkl 보존"
else
  rm -rf "$D/shape/matching" "$D/shape/masked_image.png" "$D/shape/object.ply" "$D/shape/visualization.mp4"
  rm -f "$D"/cotracker/*_30fps.mp4 "$D"/cotracker/*_vischeck.mp4 "$D"/cotracker/*_maskcheck.mp4 \
        "$D"/cotracker/hand/*_30fps.mp4 "$D"/cotracker/hand/*_vischeck.mp4 2>/dev/null
  rm -f "$D/hand_track_data.pkl" "$D/viz_sam_masks.mp4" 2>/dev/null
fi

echo "########## 3.5/4 normalize: 모든 mp4 H.264 -> mpeg4 (H.264 완전 제거 보장) ##########"
find "$D" -name "*.mp4" 2>/dev/null | while read -r v; do
  # </dev/null: ffmpeg/ffprobe otherwise read stdin and swallow the rest of the file list
  # this loop is reading, so every other file was skipped and H.264 survived into the zip.
  codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of default=nk=1:nw=1 "$v" </dev/null 2>/dev/null)
  if [ "$codec" = "h264" ]; then
    tmp="${v%.mp4}__m4.mp4"
    ffmpeg -nostdin -y -i "$v" -c:v mpeg4 -q:v 3 -pix_fmt yuv420p "$tmp" >/dev/null 2>&1 && mv -f "$tmp" "$v" && echo "  reencoded h264->mpeg4: ${v#$D/}"
  fi
done

echo "########## 3.9/4 dataset metadata xlsx (append/update this episode's row) ##########"
# needs openpyxl: KP_PY_XLSX (any python with openpyxl), else the phystwin env. Human-typed
# cells (Contact Type, 비고) are never overwritten, so re-running is safe.
"${KP_PY_XLSX:-$KP_PY_PHYSTWIN}" data_process/export_dataset_metadata_xlsx.py --base_path "$BP" --case "$D" || echo "  (xlsx 갱신 실패 — 전달물에는 영향 없음)"

echo "########## 4/4 zip ##########"
# under --keep the extras stay on disk but are excluded here, so the shipped zip is the same
# delivery format as a normal run.
if [ $KEEP -eq 1 ]; then
  ( cd "$BP" && rm -f "$EP.zip" && zip -r -q "$EP.zip" "$EP" \
      -x "$EP/shape/matching/*" "$EP/shape/masked_image.png" "$EP/shape/object.ply" \
         "$EP/shape/visualization.mp4" "$EP/cotracker/*_30fps.mp4" "$EP/cotracker/*_vischeck.mp4" \
         "$EP/cotracker/*_maskcheck.mp4" "$EP/cotracker/hand/*_30fps.mp4" \
         "$EP/cotracker/hand/*_vischeck.mp4" "$EP/hand_track_data.pkl" "$EP/viz_sam_masks.mp4" \
         "$EP/additional/*" "$EP/cotracker_hand/*" "$EP/final_data.mp4" "$EP/final_pcd.mp4" \
         "$EP/review_4cam.mp4" )
else
  ( cd "$BP" && rm -f "$EP.zip" && zip -r -q "$EP.zip" "$EP" )
fi
echo "########## DONE $EP  zip=$(ls -lh $BP/$EP.zip 2>/dev/null | awk '{print $5}') ##########"
