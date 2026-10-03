"""
batch_vr180_60deg.py
=====================
Batch-convert stereo MPO photos (FujiFilm REAL 3D W1 / similar dual-lens
cameras) into equirectangular image pairs sized for a narrow canvas FOV
(default 60 degrees -- matches VR180 Creator's manual "FOV" override
setting), and repackage the pair back into a single .MPO container.

The output MPO mirrors the source file's own MPF container structure as
closely as possible (Index IFD + Attribute IFD + per-image Exif), so that
strict MPO readers -- e.g. StereoPhoto Maker -- can open it, not just
lenient ones like Pillow.

Design goals
------------
* No wasted black canvas: canvas covers only `--fov-canvas` degrees, sized
  so the real photographed content keeps ~source pixel density (no
  needless upsampling/downsampling).
* Works for any source resolution: lens FOV is re-derived PER FILE from
  that file's own EXIF (FocalLength + FocalPlane resolution/unit), so a
  mix of different photos/resolutions in one folder is handled correctly.
* EXIF is carried over byte-for-byte (DateTimeOriginal, Make/Model, ...),
  only the width/height fields are patched to the new canvas size.
* The MPF "Attribute IFD" (stereo baseline length / convergence angle) is
  copied byte-for-byte from the source when its layout matches ours --
  this data describes the physical camera geometry, which reprojection
  does not change, so it stays valid. If a source file's structure is
  unrecognized this step is skipped gracefully (Index IFD alone is still
  written, which is spec-valid).

Requirements (install once):
    pip install opencv-python numpy pillow

Usage
-----
    python batch_vr180_60deg.py --src "C:\\path\\to\\photos" --out "C:\\path\\to\\output"
    python batch_vr180_60deg.py --src ./photos --out ./converted --fov-canvas 60 --quality 100

Run with --help for all options.
"""
import argparse
import glob
import math
import os
import struct

import cv2
import numpy as np
from PIL import Image
from PIL.ExifTags import TAGS

# ------------------------------------------------------------------
# Fallback lens FOV, only used if a file's EXIF doesn't contain enough
# info (FocalLength + FocalPlane resolution) to derive it directly.
# ------------------------------------------------------------------
DEFAULT_FOV_H_DEG = 51.9
DEFAULT_FOV_V_DEG = 30.6

# FocalPlaneResolutionUnit -> mm-per-unit
_RES_UNIT_TO_MM = {2: 25.4, 3: 10.0, 4: 1.0, 5: 0.001}  # 2=inch,3=cm,4=mm,5=um


# ============================================================
# 1. Per-file lens FOV
# ============================================================
def get_lens_fov(path):
    try:
        im = Image.open(path)
        exif = im._getexif()
        if not exif:
            print(f"  [warn] no EXIF found, using default FOV "
                  f"{DEFAULT_FOV_H_DEG}x{DEFAULT_FOV_V_DEG} deg")
            return DEFAULT_FOV_H_DEG, DEFAULT_FOV_V_DEG
        tags = {TAGS.get(k, k): v for k, v in exif.items()}
        focal = tags.get('FocalLength')
        fpx_res = tags.get('FocalPlaneXResolution')
        fpy_res = tags.get('FocalPlaneYResolution')
        res_unit = tags.get('FocalPlaneResolutionUnit', 2)
        w = tags.get('ExifImageWidth', im.size[0])
        h = tags.get('ExifImageHeight', im.size[1])
        if focal and fpx_res and fpy_res:
            mm_per_unit = _RES_UNIT_TO_MM.get(int(res_unit), 25.4)
            sensor_w_mm = float(w) / float(fpx_res) * mm_per_unit
            sensor_h_mm = float(h) / float(fpy_res) * mm_per_unit
            fov_h = math.degrees(2 * math.atan((sensor_w_mm / 2.0) / float(focal)))
            fov_v = math.degrees(2 * math.atan((sensor_h_mm / 2.0) / float(focal)))
            return fov_h, fov_v
    except Exception as e:
        print(f"  [warn] could not read EXIF FOV ({e}), using default")
    return DEFAULT_FOV_H_DEG, DEFAULT_FOV_V_DEG


# ============================================================
# 2. Parse source MPO: split raw JPEG streams + grab MPF Index/
#    Attribute IFD info for later reuse.
# ============================================================
def _parse_mpf_app2(data):
    """Locate and parse the first APP2 'MPF' segment. Returns dict with
    tiff bytes, endian, ifd0 entries/offset, mpentry offset+entries,
    and (if present) the raw attribute-IFD byte blob + its tiff offset."""
    pos = 2
    app2_pos = app2_len = None
    while pos < len(data) - 4 and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xDA:
            break
        seg_len = int.from_bytes(data[pos + 2:pos + 4], 'big')
        if marker == 0xE2 and data[pos + 4:pos + 8] == b'MPF\x00':
            app2_pos, app2_len = pos, seg_len
            break
        pos += 2 + seg_len

    if app2_pos is None:
        return None

    payload = data[app2_pos + 4: app2_pos + 2 + app2_len]
    tiff = payload[4:]
    endian = '<' if tiff[0:2] == b'II' else '>'
    ifd0_offset = struct.unpack(endian + 'I', tiff[4:8])[0]

    count = struct.unpack(endian + 'H', tiff[ifd0_offset:ifd0_offset + 2])[0]
    p = ifd0_offset + 2
    entries = []
    mpentry_offset = None
    for _ in range(count):
        tag, typ, cnt, val = struct.unpack(endian + 'HHI4s', tiff[p:p + 12])
        entries.append((tag, typ, cnt, val))
        if tag == 0xb002:
            mpentry_offset = struct.unpack(endian + 'I', val)[0]
        p += 12
    next_ifd = struct.unpack(endian + 'I', tiff[p:p + 4])[0]

    if mpentry_offset is None:
        return None

    e0 = struct.unpack(endian + 'IIIHH', tiff[mpentry_offset:mpentry_offset + 16])
    e1 = struct.unpack(endian + 'IIIHH', tiff[mpentry_offset + 16:mpentry_offset + 32])

    attribute_ifd_blob = None
    if next_ifd:
        attribute_ifd_blob = bytes(tiff[next_ifd:])

    return {
        'app2_pos': app2_pos,
        'tiff_abs_start': app2_pos + 4 + 4,
        'entry0': e0,
        'entry1': e1,
        'ifd0_next_offset': next_ifd,
        'attribute_ifd_blob': attribute_ifd_blob,
    }


def split_mpo_raw(path):
    """Split an MPO file into its raw JPEG byte streams (no re-encoding)."""
    data = open(path, 'rb').read()
    assert data[0:2] == b'\xff\xd8', f"{path} is not a JPEG/MPO file"

    info = _parse_mpf_app2(data)
    if info is None:
        raise RuntimeError(f"Could not parse MPF index in {path}; unsupported MPO layout")

    e0, e1 = info['entry0'], info['entry1']
    tiff_abs_start = info['tiff_abs_start']
    img1 = data[0:e0[1]]
    img2_start = tiff_abs_start + e1[2]
    img2 = data[img2_start:img2_start + e1[1]]
    return img1, img2, info


def extract_app_segment(jpeg_bytes, marker, identifier):
    """Return the full segment (marker+len+payload) matching `marker` byte
    whose payload starts with `identifier`, or None if absent."""
    pos = 2
    while pos < len(jpeg_bytes) - 4 and jpeg_bytes[pos] == 0xFF:
        m = jpeg_bytes[pos + 1]
        if m == 0xDA:
            break
        seg_len = int.from_bytes(jpeg_bytes[pos + 2:pos + 4], 'big')
        if m == marker and jpeg_bytes[pos + 4:pos + 4 + len(identifier)] == identifier:
            return jpeg_bytes[pos:pos + 2 + seg_len]
        pos += 2 + seg_len
    return None


# ============================================================
# 3. Exif carry-over (byte-for-byte, only dimensions patched)
# ============================================================
def extract_exif_app1(jpeg_bytes):
    pos = 2
    while pos < len(jpeg_bytes) - 4 and jpeg_bytes[pos] == 0xFF:
        marker = jpeg_bytes[pos + 1]
        if marker == 0xDA:
            break
        seg_len = int.from_bytes(jpeg_bytes[pos + 2:pos + 4], 'big')
        if marker == 0xE1 and jpeg_bytes[pos + 4:pos + 10] == b'Exif\x00\x00':
            return jpeg_bytes[pos:pos + 2 + seg_len]
        pos += 2 + seg_len
    return None


def patch_exif_dimensions(app1_segment, new_w, new_h):
    seg = bytearray(app1_segment)
    tiff_start = 4 + 6  # marker(2)+len(2) + "Exif\0\0"(6)
    tiff_buf = bytearray(seg[tiff_start:])
    endian = '<' if bytes(tiff_buf[0:2]) == b'II' else '>'
    ifd0_offset = struct.unpack(endian + 'I', bytes(tiff_buf[4:8]))[0]

    exif_subifd_offset = None

    def scan_and_patch(ifd_offset, wh_tags):
        nonlocal exif_subifd_offset
        count = struct.unpack(endian + 'H', tiff_buf[ifd_offset:ifd_offset + 2])[0]
        p = ifd_offset + 2
        for _ in range(count):
            tag, typ, cnt = struct.unpack(endian + 'HHI', tiff_buf[p:p + 8])
            val_off = p + 8
            if tag == 0x8769:
                exif_subifd_offset = struct.unpack(endian + 'I', bytes(tiff_buf[val_off:val_off + 4]))[0]
            if tag in wh_tags and cnt == 1:
                new_val = wh_tags[tag]
                if typ == 3:
                    struct.pack_into(endian + 'H', tiff_buf, val_off, new_val)
                    struct.pack_into(endian + 'H', tiff_buf, val_off + 2, 0)
                elif typ == 4:
                    struct.pack_into(endian + 'I', tiff_buf, val_off, new_val)
            p += 12

    scan_and_patch(ifd0_offset, {0x0100: new_w, 0x0101: new_h})
    if exif_subifd_offset is not None:
        scan_and_patch(exif_subifd_offset, {0xA002: new_w, 0xA003: new_h})

    new_seg = bytes(seg[:tiff_start]) + bytes(tiff_buf)
    assert len(new_seg) == len(app1_segment)
    return new_seg


# ============================================================
# 4. Perspective -> equirectangular remap
# ============================================================
def build_equirect_map(fov_h_deg, src_w, src_h, canvas_size, canvas_fov_deg):
    fov_h = math.radians(fov_h_deg)
    f = (src_w / 2.0) / math.tan(fov_h / 2.0)

    half_span = math.radians(canvas_fov_deg) / 2.0
    u = np.linspace(0, canvas_size - 1, canvas_size)
    v = np.linspace(0, canvas_size - 1, canvas_size)
    uu, vv = np.meshgrid(u, v)

    theta = (uu / canvas_size - 0.5) * (2 * half_span)
    phi = (0.5 - vv / canvas_size) * (2 * half_span)

    x = np.cos(phi) * np.sin(theta)
    y = np.sin(phi)
    z = np.cos(phi) * np.cos(theta)

    valid = z > 1e-6
    px = np.zeros_like(x)
    py = np.zeros_like(y)
    px[valid] = x[valid] / z[valid]
    py[valid] = y[valid] / z[valid]

    map_x = (src_w / 2.0 + f * px).astype(np.float32)
    map_y = (src_h / 2.0 - f * py).astype(np.float32)

    out_of_range = (map_x < 0) | (map_x >= src_w) | (map_y < 0) | (map_y >= src_h) | (~valid)
    map_x[out_of_range] = -1
    map_y[out_of_range] = -1
    return map_x, map_y


def compute_canvas_size(fov_h_deg, fov_v_deg, src_w, src_h, canvas_fov_deg):
    px_per_deg = max(src_w / fov_h_deg, src_h / fov_v_deg)
    raw = px_per_deg * canvas_fov_deg
    return ((int(raw) // 16) + 1) * 16


# ============================================================
# 5. MPO (MPF) container assembly
# ============================================================
def make_app_segment(payload_bytes, marker):
    seg_len = len(payload_bytes) + 2
    if seg_len > 65535:
        raise ValueError("segment too large for a single APP marker (needs splitting)")
    return bytes([0xFF, marker]) + struct.pack('>H', seg_len) + payload_bytes


# Our fixed Index-IFD layout always has exactly 3 entries (Version,
# NumberOfImages, MPEntry) + 2 MP-entries -> the Attribute IFD (if any)
# always lands at this fixed relative tiff offset.
_FIXED_ATTRIBUTE_IFD_OFFSET = 8 + 2 + (3 * 12) + 4 + 32  # = 82


def build_mpf_app2(img1_total_size, img2_size, img2_dataoffset_from_tiff,
                    attribute_ifd_blob=None):
    endian = '<'
    tiff_header = b'II' + struct.pack(endian + 'H', 42) + struct.pack(endian + 'I', 8)
    mpentry_rel_offset = 8 + 2 + (3 * 12) + 4  # 50

    ifd0 = struct.pack(endian + 'H', 3)
    ifd0 += struct.pack(endian + 'HHI4s', 0xb000, 7, 4, b'0100')
    ifd0 += struct.pack(endian + 'HHI4s', 0xb001, 4, 1, struct.pack(endian + 'I', 2))
    ifd0 += struct.pack(endian + 'HHI4s', 0xb002, 7, 32, struct.pack(endian + 'I', mpentry_rel_offset))
    next_ifd_val = _FIXED_ATTRIBUTE_IFD_OFFSET if attribute_ifd_blob else 0
    ifd0 += struct.pack(endian + 'I', next_ifd_val)

    entry0 = struct.pack(endian + 'IIIHH', 0x20020002, img1_total_size, 0, 0, 0)
    entry1 = struct.pack(endian + 'IIIHH', 0x00020002, img2_size, img2_dataoffset_from_tiff, 0, 0)

    tiff = tiff_header + ifd0 + entry0 + entry1
    assert len(tiff) == _FIXED_ATTRIBUTE_IFD_OFFSET
    if attribute_ifd_blob:
        tiff += attribute_ifd_blob

    payload = b'MPF\x00' + tiff
    return make_app_segment(payload, 0xE2)


def strip_leading_app0(jpeg_bytes):
    if len(jpeg_bytes) > 4 and jpeg_bytes[2] == 0xFF and jpeg_bytes[3] == 0xE0:
        seg_len = int.from_bytes(jpeg_bytes[4:6], 'big')
        return jpeg_bytes[:2] + jpeg_bytes[2 + 2 + seg_len:]
    return jpeg_bytes


def repackage_as_mpo(img1_body_after_soi, exif1_segment, img2_body_after_soi,
                      exif2_segment, img2_mpf_segment, out_path,
                      attribute_ifd_blob=None):
    """img1/img2_body_after_soi: encoded JPEG bytes with any leading APP0
    already stripped (starts with SOI, then straight to DQT/SOF/...).
    exif1/exif2_segment: optional full Exif APP1 segments (or None).
    img2_mpf_segment: optional full MPF APP2 segment for image2 -- this is
    pure per-image metadata (MPIndividualNum etc.), not offset-dependent,
    so it can be reused unmodified from the source. Strict MPO readers
    (e.g. StereoPhoto Maker) expect BOTH images to carry Exif+MPF, not
    just the primary/representative one."""
    soi1 = img1_body_after_soi[:2]
    rest1 = img1_body_after_soi[2:]
    header1 = soi1 + (exif1_segment if exif1_segment else b'')
    app2_marker_pos = len(header1)
    tiff_abs_start = app2_marker_pos + 4 + 4

    placeholder = build_mpf_app2(0, 0, 0, attribute_ifd_blob)
    img1_total_size = len(header1) + len(placeholder) + len(rest1)

    soi2 = img2_body_after_soi[:2]
    rest2 = img2_body_after_soi[2:]
    header2 = soi2 + (exif2_segment if exif2_segment else b'') + (img2_mpf_segment if img2_mpf_segment else b'')
    img2_full = header2 + rest2
    img2_size = len(img2_full)

    img2_dataoffset_from_tiff = img1_total_size - tiff_abs_start
    app2 = build_mpf_app2(img1_total_size, img2_size, img2_dataoffset_from_tiff,
                           attribute_ifd_blob)
    assert len(app2) == len(placeholder)

    final_img1 = header1 + app2 + rest1
    assert len(final_img1) == img1_total_size

    with open(out_path, 'wb') as f:
        f.write(final_img1)
        f.write(img2_full)


# ============================================================
# 6. Per-file pipeline
# ============================================================
def process_one(mpo_path, out_dir, canvas_fov_deg, quality, interp):
    name = os.path.splitext(os.path.basename(mpo_path))[0]
    print(f"\n=== {mpo_path} ===")

    fov_h, fov_v = get_lens_fov(mpo_path)
    print(f"  lens FOV: {fov_h:.2f} x {fov_v:.2f} deg")

    left_raw, right_raw, mpf_info = split_mpo_raw(mpo_path)
    left_img = cv2.imdecode(np.frombuffer(left_raw, np.uint8), cv2.IMREAD_COLOR)
    right_img = cv2.imdecode(np.frombuffer(right_raw, np.uint8), cv2.IMREAD_COLOR)
    if left_img is None or right_img is None:
        raise RuntimeError(f"Failed to decode stereo pair in {mpo_path}")
    src_h, src_w = left_img.shape[:2]
    print(f"  source resolution: {src_w}x{src_h}")

    canvas_size = compute_canvas_size(fov_h, fov_v, src_w, src_h, canvas_fov_deg)
    print(f"  canvas: {canvas_size}x{canvas_size} px (covers {canvas_fov_deg} deg)")

    map_x, map_y = build_equirect_map(fov_h, src_w, src_h, canvas_size, canvas_fov_deg)

    left_eq = cv2.remap(left_img, map_x, map_y, interpolation=interp,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    right_eq = cv2.remap(right_img, map_x, map_y, interpolation=interp,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))

    encode_params = [
        cv2.IMWRITE_JPEG_QUALITY, quality,
        cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444,  # no chroma subsampling
        cv2.IMWRITE_JPEG_OPTIMIZE, 1,  # lossless huffman-table optimization
    ]
    ok1, left_enc = cv2.imencode('.jpg', left_eq, encode_params)
    ok2, right_enc = cv2.imencode('.jpg', right_eq, encode_params)
    if not (ok1 and ok2):
        raise RuntimeError(f"JPEG encoding failed for {mpo_path}")

    left_bytes = strip_leading_app0(left_enc.tobytes())
    right_bytes = strip_leading_app0(right_enc.tobytes())

    # ---- carry over original EXIF (image1 / representative eye) ----
    exif1_seg = extract_exif_app1(left_raw)
    if exif1_seg:
        try:
            exif1_seg = patch_exif_dimensions(exif1_seg, canvas_size, canvas_size)
            print("  image1 EXIF: carried over from source, dimensions patched")
        except Exception as e:
            print(f"  [warn] image1 EXIF dimension patch failed ({e}); keeping as-is")
    else:
        print("  [warn] no Exif block found in source image1; output will have no EXIF there")

    # ---- carry over image2's own EXIF + MPF (StereoPhoto Maker requires
    #      BOTH images to have these, not just the representative one) ----
    exif2_seg = extract_app_segment(right_raw, 0xE1, b'Exif\x00\x00')
    if exif2_seg:
        try:
            exif2_seg = patch_exif_dimensions(exif2_seg, canvas_size, canvas_size)
            print("  image2 EXIF: carried over from source, dimensions patched")
        except Exception as e:
            print(f"  [warn] image2 EXIF dimension patch failed ({e}); keeping as-is")
    else:
        print("  [warn] no Exif block found in source image2")

    img2_mpf_seg = extract_app_segment(right_raw, 0xE2, b'MPF\x00')
    if img2_mpf_seg:
        print("  image2 MPF (MPIndividualNum/baseline metadata): carried over from source")
    else:
        print("  [warn] no MPF block found in source image2 "
              "(some strict MPO readers, e.g. StereoPhoto Maker, may then refuse to open the file)")

    # ---- carry over Attribute IFD (baseline length / convergence angle) ----
    attribute_ifd_blob = None
    if (mpf_info.get('attribute_ifd_blob') and
            mpf_info.get('ifd0_next_offset') == _FIXED_ATTRIBUTE_IFD_OFFSET):
        attribute_ifd_blob = mpf_info['attribute_ifd_blob']
        print("  image1 MPF Attribute IFD (stereo baseline/convergence): carried over from source")
    elif mpf_info.get('attribute_ifd_blob'):
        print("  [warn] source Attribute IFD layout differs from expected; skipping "
              "(Index IFD alone is still spec-valid)")

    out_path = os.path.join(out_dir, f"{name}_{int(canvas_fov_deg)}deg.MPO")
    repackage_as_mpo(left_bytes, exif1_seg, right_bytes, exif2_seg, img2_mpf_seg,
                      out_path, attribute_ifd_blob)
    print(f"  -> wrote {out_path} "
          f"({os.path.getsize(out_path):,} bytes, source was {os.path.getsize(mpo_path):,} bytes)")
    return out_path


# ============================================================
# 7. CLI
# ============================================================
def main():
    ap = argparse.ArgumentParser(
        description="Batch-convert stereo MPO photos to narrow-FOV equirectangular MPO pairs for VR180 Creator.")
    ap.add_argument('--src', default='.', help="Folder containing .MPO files (default: current folder)")
    ap.add_argument('--out', default='./vr180_out', help="Output folder (default: ./vr180_out)")
    ap.add_argument('--fov-canvas', type=float, default=60.0,
                     help="Canvas FOV in degrees, must match the FOV you set in VR180 Creator (default: 60)")
    ap.add_argument('--quality', type=int, default=100,
                     help="Output JPEG quality 1-100 (default: 100, no chroma subsampling used "
                          "either way to keep the actual photo content as close to source fidelity as possible)")
    ap.add_argument('--fast', action='store_true',
                     help="Use bilinear interpolation instead of Lanczos4 (faster, slightly softer)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    interp = cv2.INTER_LINEAR if args.fast else cv2.INTER_LANCZOS4

    mpo_files = sorted(set(
        glob.glob(os.path.join(args.src, '*.MPO')) +
        glob.glob(os.path.join(args.src, '*.mpo'))
    ))
    # skip our own outputs (<name>_<N>deg.*) to avoid double conversion
    suffix = f"_{int(args.fov_canvas)}deg"
    skipped = [p for p in mpo_files if os.path.splitext(os.path.basename(p))[0].endswith(suffix)]
    mpo_files = [p for p in mpo_files if p not in skipped]
    for p in skipped:
        print(f"skip (already converted): {p}")
    print(f"found {len(mpo_files)} MPO file(s) in {args.src}")

    ok, failed = [], []
    for p in mpo_files:
        try:
            ok.append(process_one(p, args.out, args.fov_canvas, args.quality, interp))
        except Exception as e:
            print(f"  [ERROR] {p}: {e}")
            failed.append(p)

    print(f"\nDone: {len(ok)} converted, {len(failed)} failed.")
    if failed:
        print("Failed files:")
        for p in failed:
            print(" -", p)


if __name__ == '__main__':
    main()
