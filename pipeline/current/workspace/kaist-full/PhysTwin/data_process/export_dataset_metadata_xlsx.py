# Append/update one row per delivered episode in the dataset metadata spreadsheet KAIST
# asked for ("(예시) Dataset Metadata.xlsx" layout):
#
#   A Data Name | B Hand Type (Single/Double) | C Single_type
#   D Contact Type (Sparse/Dense) | E ID | F Object | G ID | H interaction | I 비고
#
# Columns are addressed by INDEX, not by header text -- E and G are both literally "ID".
# C (Single_type) is ours, inserted into KAIST's 7-column template; a sheet still in the old
# layout is migrated on open (insert_cols), so typed cells survive.
#
# HUMAN EDITS WIN. The script only writes a cell when it has a value for it; anything it
# cannot derive (Contact Type) is left for a person to type, and re-running never
# overwrites what a person typed. That is what makes it safe to hook into the delivery run.
#
# What is derived vs. not:
#   Data Name   <- case dir name
#   Object      <- {case}/metadata.json "object_name", title-cased
#   Hand Type   <- metadata.json "human_hand_type", set in the SAM mask editor. The
#                  camera-support verdict is still computed and printed next to it so a
#                  disagreement is visible, but it is NOT written to the sheet.
#   Single_type <- "human_single_type" ('right'/'left'); 'None' for a two-handed episode
#   Contact Type<- "human_contact_type" ('sparse'/'dense'). Not derivable; set by a person.
#   E / G "ID"  <- integer code per distinct Contact Type / Object value. Codes are read back
#                  out of the sheet, so an existing code is reused and a human-assigned code
#                  is respected; a value never seen before gets max+1.
#   interaction <- metadata.json "human_interaction", typed per episode in the SAM mask
#                  editor (sam_mask_refine.py). Blank until someone reviews that episode.
#   비고        <- which delivery archive this row shipped in; pass --zip_name. Left alone
#                  when not given, so re-running for another reason cannot blank it.
#
# Usage (system python3; openpyxl is not installed in the phystwin env):
#   python3 data_process/export_dataset_metadata_xlsx.py --case ./data/different_types/ep_XXXX
#   python3 data_process/export_dataset_metadata_xlsx.py --all      # every delivered case

import os
import re
import json
import glob
import argparse

# Single_type sits next to Hand Type: which hand a one-handed episode used ('right'/'left'),
# 'None' when the episode is two-handed. It is a separate column rather than a 'single_left'
# variant of Hand Type so that Hand Type stays the two-valued field KAIST's template defines.
HEADERS = ["Data Name", "Hand Type (Single/Double)", "Single_type",
           "Contact Type (Sparse/Dense)", "ID", "Object", "ID", "interaction", "비고"]
(COL_NAME, COL_HAND, COL_SINGLE_TYPE, COL_CONTACT,
 COL_CONTACT_ID, COL_OBJECT, COL_OBJECT_ID, COL_INTERACTION, COL_NOTE) = range(1, 10)

HAMBA_OUT = "Mono/data_process/hand_recon_out"


def hand_type(case, case_name, hamba_out=HAMBA_OUT):
    """-> (hand_type, single_type): ('single','right'|'left') or ('double', None).

    Preference order, best evidence first. The first two sources are plain numpy pickles, so
    this runs under a bare python3 with only openpyxl -- no env has both torch and openpyxl,
    and the delivery hook needs openpyxl.

      1. shape/hand/hand_mano_trajectory.pkl  the MANO hands actually FITTED (what we ship)
      2. {log}/hand_reg.pkl                   same thing, raw Mono stage-2 output
      3. hand_recon_out/*/results.pt          HaMBA raw DETECTIONS (torch only; a detection
                                              is not a fit, so this is the weakest source)

    "double" when a majority of usable frames carry two hands.
    """
    import pickle

    def _majority(counts):
        if not counts:
            return None, None
        n2 = sum(1 for c in counts if c >= 2)
        return ("double", None) if n2 * 2 > len(counts) else ("single", None)

    # 1) our own delivery artefact. export_hand_mano_trajectory.py has already dropped hands
    # no camera ever observed and recorded the verdict, including which hand a one-handed
    # episode used -- 'single' (right) vs 'single_left'. Episodes are always FITTED as
    # two-handed, so this label comes from the result, never from the run configuration.
    p = f"{case}/shape/hand/hand_mano_trajectory.pkl"
    if os.path.isfile(p):
        try:
            d = pickle.load(open(p, "rb"))
            ht = d.get("hand_type")
            if ht:
                st = d.get("single_type")
                return str(ht), (str(st) if st else None)
            valid = d["mano_valid"]                       # (T, H) bool
            return _majority([int(sum(r)) for r in valid if any(r)])
        except Exception as e:
            print(f"  [warn] {p}: {e}")

    # 2) raw stage-2 fit
    for p in sorted(glob.glob(f"{case}/*/hand_reg.pkl")):
        try:
            reg = pickle.load(open(p, "rb"))
        except Exception as e:
            print(f"  [warn] {p}: {e}")
            continue
        counts = []
        for entry in reg.values():
            if isinstance(entry, dict):
                n = sum(1 for v in entry.values() if v is not None)
                if n:
                    counts.append(n)
        r = _majority(counts)
        if r[0]:
            return r

    # 3) HaMBA detections -- only if torch happens to be importable
    cams = sorted(glob.glob(f"{hamba_out}/{case_name}/*/results.pt"))
    if not cams:
        return None, None
    try:
        import torch
    except ImportError:
        return None, None
    per_frame = {}
    for p in cams:
        try:
            d = torch.load(p, weights_only=False)
        except Exception as e:
            print(f"  [warn] {p}: {e}")
            continue
        seen = {}
        for k, v in d.items():
            if v is None:
                continue
            f, h = k.rsplit("_", 1)
            seen.setdefault(f, set()).add(h)
        for f, hs in seen.items():
            per_frame[f] = max(per_frame.get(f, 0), len(hs))
    return _majority(list(per_frame.values()))


def _load_sheet(path):
    import openpyxl
    if os.path.isfile(path):
        wb = openpyxl.load_workbook(path)
        ws = wb.worksheets[0]
        # migrate a sheet written before Single_type existed: open a gap next to Hand Type
        # so every already-typed cell keeps its value, just shifted right
        if str(ws.cell(row=1, column=COL_SINGLE_TYPE).value or "").strip() != "Single_type":
            ws.insert_cols(COL_SINGLE_TYPE)
            ws.cell(row=1, column=COL_SINGLE_TYPE, value="Single_type")
            print(f"  [migrate] inserted 'Single_type' column at {COL_SINGLE_TYPE}")
        # migrate a sheet whose last column was '비고' holding the interaction text: that
        # column becomes 'interaction' and a fresh '비고' (which delivery zip the row shipped
        # in) is appended after it. Cell values move with the header, so nothing is lost.
        if str(ws.cell(row=1, column=COL_INTERACTION).value or "").strip() != "interaction":
            ws.cell(row=1, column=COL_INTERACTION, value="interaction")
            ws.cell(row=1, column=COL_NOTE, value="비고")
            print(f"  [migrate] column {COL_INTERACTION} -> 'interaction', "
                  f"added '비고' at {COL_NOTE}")
        return wb, ws
    wb = openpyxl.Workbook()
    ws = wb.worksheets[0]
    for i, h in enumerate(HEADERS, start=1):
        ws.cell(row=1, column=i, value=h)
    return wb, ws


def _find_row(ws, data_name):
    for r in range(2, ws.max_row + 1):
        if str(ws.cell(row=r, column=COL_NAME).value or "").strip() == data_name:
            return r
    return None


def _code_for(ws, value_col, id_col, value):
    """Integer code for `value`, reusing whatever the sheet already assigns to it.

    Keeps codes stable across runs and respects codes a human typed. A value the sheet has
    never carried gets max(existing)+1.
    """
    if not value:
        return None
    used, existing = set(), None
    for r in range(2, ws.max_row + 1):
        v = str(ws.cell(row=r, column=value_col).value or "").strip().lower()
        c = ws.cell(row=r, column=id_col).value
        if c is None or str(c).strip() == "":
            continue
        try:
            c = int(float(c))
        except (TypeError, ValueError):
            continue
        used.add(c)
        if v == value.strip().lower():
            existing = c
    if existing is not None:
        return existing
    return (max(used) + 1) if used else 0


def _set(ws, row, col, value, overwrite=False):
    """Write only when we have a value and (the cell is empty or overwrite)."""
    if value in (None, ""):
        return False
    cur = ws.cell(row=row, column=col).value
    if not overwrite and cur not in (None, "") and str(cur).strip() != "":
        return False
    ws.cell(row=row, column=col, value=value)
    return True


def upsert(ws, case, hamba_out=HAMBA_OUT, zip_name=None):
    name = os.path.basename(case.rstrip("/"))
    meta_p = f"{case}/metadata.json"
    if not os.path.isfile(meta_p):
        print(f"  [skip] {name}: no metadata.json")
        return False
    meta = json.load(open(meta_p))
    obj = (meta.get("object_name") or "").strip()
    obj = re.sub(r"\s+", " ", obj).title() or None
    h_hand = (meta.get("human_hand_type") or "").strip()
    h_single = (meta.get("human_single_type") or "").strip()
    h_contact = (meta.get("human_contact_type") or "").strip()

    row = _find_row(ws, name)
    new = row is None
    if new:
        row = ws.max_row + 1 if ws.max_row >= 2 or ws.cell(row=1, column=1).value else 2
        ws.cell(row=row, column=COL_NAME, value=name)

    # object + its code: overwrite, both are authoritative from metadata.json
    _set(ws, row, COL_OBJECT, obj, overwrite=True)
    _set(ws, row, COL_OBJECT_ID, _code_for(ws, COL_OBJECT, COL_OBJECT_ID, obj), overwrite=True)

    # 비고 = metadata.json "human_interaction": what the person reviewing this episode's masks
    # in sam_mask_refine.py typed that it actually does. NOT "interaction", which is the
    # per-object boilerplate from data_process/episodes.py (identical across every episode of
    # the same object) and is rewritten by finalize_delivery on each delivery run.
    # Left alone when absent, so an episode still awaiting review keeps whatever is there.
    _set(ws, row, COL_INTERACTION, (meta.get("human_interaction") or "").strip(), overwrite=True)
    if zip_name:
        _set(ws, row, COL_NOTE, f"{name} / {zip_name}", overwrite=True)

    # Hand Type / Single_type / Contact Type come from what a person set in the SAM mask
    # editor (metadata.json human_* keys), not from the derived camera-support verdict. The
    # derived value is still computed and printed so a mismatch is visible, but it is not
    # written: a label the reviewer looked at the footage to set outranks it.
    ht, st = hand_type(case, name, hamba_out)          # derived, for the mismatch note only
    _set(ws, row, COL_HAND, h_hand, overwrite=True)
    # 'None' written literally: a two-handed episode HAS no single_type, which is different
    # from a one-handed episode whose side has not been set yet (blank)
    if h_hand:
        _set(ws, row, COL_SINGLE_TYPE, (h_single or "None") if h_hand == "single" else "None",
             overwrite=True)

    _set(ws, row, COL_CONTACT, h_contact, overwrite=True)
    contact = str(ws.cell(row=row, column=COL_CONTACT).value or "").strip()
    if contact:
        _set(ws, row, COL_CONTACT_ID,
             _code_for(ws, COL_CONTACT, COL_CONTACT_ID, contact), overwrite=True)

    todo = [lbl for lbl, v in (("Hand Type", h_hand), ("Contact Type", h_contact),
                               ("비고(interaction)", meta.get("human_interaction")))
            if not str(v or "").strip()]
    tag = "added" if new else "updated"
    mism = (f"   [불일치: 사람={h_hand} vs 자동={ht}]"
            if h_hand and ht and h_hand != ht else "")
    print(f"  {tag}: {name}  object={obj}  hand={h_hand or '-'}"
          f"{f' ({h_single})' if h_single else ''}  contact={h_contact or '-'}"
          f"  (자동판정 hand={ht or '-'})" + mism
          + (f"   [SAM 에디터 입력 필요: {', '.join(todo)}]" if todo else ""))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", help="one case dir, e.g. ./data/different_types/ep_XXXX")
    ap.add_argument("--all", action="store_true", help="every case under --base_path")
    ap.add_argument("--base_path", default="./data/different_types")
    ap.add_argument("--xlsx", default=None, help="default: {base_path}/dataset_metadata.xlsx")
    ap.add_argument("--hamba_out", default=HAMBA_OUT)
    ap.add_argument("--zip_name", default=None,
                    help="delivery archive this run's rows ship in; written to 비고 as "
                         "'<Data Name> / <zip>'. Omitted -> 비고 left untouched.")
    args = ap.parse_args()

    if not args.case and not args.all:
        raise SystemExit("need --case or --all")

    xlsx = args.xlsx or f"{args.base_path}/dataset_metadata.xlsx"
    wb, ws = _load_sheet(xlsx)

    cases = ([args.case] if args.case else
             sorted(d for d in glob.glob(f"{args.base_path}/*")
                    if os.path.isfile(f"{d}/metadata.json")))
    n = sum(bool(upsert(ws, c.rstrip("/"), args.hamba_out, args.zip_name)) for c in cases)

    os.makedirs(os.path.dirname(xlsx) or ".", exist_ok=True)
    wb.save(xlsx)
    print(f"[done] {n} row(s) -> {xlsx}")


if __name__ == "__main__":
    main()
