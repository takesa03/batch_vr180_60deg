"""
equirect_mono_60deg.py
=======================
Batch-convert regular (single-lens, non-stereo) digital camera JPEG photos
into a narrow-FOV equirectangular JPEG, sized for VR180 Creator's manual
"FOV" override setting (default 60 degrees).

This is the single-image counterpart to batch_vr180_60deg.py (which
handles stereo MPO pairs).

FOV model
---------
This tool does NOT try to reproduce the camera's real optical field of
view. Instead, the whole photo is treated as a flat "virtual screen"
placed in VR space, spanning exactly `--fov-canvas` degrees horizontally
(default 60) -- filling the canvas width edge-to-edge, with no
real-optics guesswork needed. The vertical extent is derived from the
photo's own aspect ratio (undistorted, standard rectilinear relation), so
a 16:9 photo naturally letterboxes within the square 60x60 canvas rather
than being stretched.

Other design points (shared with the MPO version)
--------------------------------------------------
* Canvas is sized to preserve source pixel density (no needless
  upsampling/downsampling), rounded to a multiple of 16.
* EXIF is carried over byte-for-byte (DateTimeOriginal, Make/Model, ...),
  only the width/height fields are patched to the new canvas size.
* Output: plain single JPEG (no MPO container -- this is mono, not stereo).
* Max-fidelity JPEG encode: quality 100, no chroma subsampling, Huffman
  optimization.

Requirements (install once):
    pip install opencv-python numpy pillow

Usage
-----
    python equirect_mono_60deg.py --src "C:\\path\\to\\photos" --out "C:\\path\\to\\output"
    python equirect_mono_60deg.py --src ./photos --out ./converted --fov-canvas 60 --quality 100

Run with --help for all options.
"""
import argparse
import glob
import math
import os
import struct

import cv2
import numpy as np


# ============================================================
# 1. Assumed FOV: horizontal = canvas FOV (fixed), vertical derived
#    from the photo's own aspect ratio. No EXIF/optics lookup at all.
# ============================================================
def get_assumed_fov(src_w, src_h, canvas_fov_deg):
    fov_h = canvas_fov_deg
    fov_v = math.degrees(2 * math.atan(math.tan(math.radians(fov_h) / 2.0) * (float(src_h) / float(src_w))))
    return fov_h, fov_v


# ============================================================
# 2. Exif carry-over (byte-for-byte, only dimensions patched)
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
            if tag == 0x8769:  # ExifOffset -> Exif SubIFD
                exif_subifd_offset = struct.unpack(endian + 'I', bytes(tiff_buf[val_off:val_off + 4]))[0]
            if tag in wh_tags and cnt == 1:
                new_val = wh_tags[tag]
                if typ == 3:    # SHORT
                    struct.pack_into(endian + 'H', tiff_buf, val_off, new_val)
                    struct.pack_into(endian + 'H', tiff_buf, val_off + 2, 0)
                elif typ == 4:  # LONG
                    struct.pack_into(endian + 'I', tiff_buf, val_off, new_val)
            p += 12

    scan_and_patch(ifd0_offset, {0x0100: new_w, 0x0101: new_h})
    if exif_subifd_offset is not None:
        scan_and_patch(exif_subifd_offset, {0xA002: new_w, 0xA003: new_h})

    new_seg = bytes(seg[:tiff_start]) + bytes(tiff_buf)
    assert len(new_seg) == len(app1_segment)
    return new_seg


def strip_leading_app0(jpeg_bytes):
    if len(jpeg_bytes) > 4 and jpeg_bytes[2] == 0xFF and jpeg_bytes[3] == 0xE0:
        seg_len = int.from_bytes(jpeg_bytes[4:6], 'big')
        return jpeg_bytes[:2] + jpeg_bytes[2 + 2 + seg_len:]
    return jpeg_bytes


def insert_exif(jpeg_bytes, exif_segment):
    """jpeg_bytes: encoded JPEG with any leading APP0 already stripped
    (starts with SOI, then straight to DQT/SOF/...)."""
    if not exif_segment:
        return jpeg_bytes
    return jpeg_bytes[:2] + exif_segment + jpeg_bytes[2:]


# ============================================================
# 3. Perspective -> equirectangular remap
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
# 4. Per-file pipeline
# ============================================================
def process_one(src_path, out_dir, canvas_fov_deg, quality, interp):
    name = os.path.splitext(os.path.basename(src_path))[0]
    print(f"\n=== {src_path} ===")

    raw = open(src_path, 'rb').read()
    src_img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if src_img is None:
        raise RuntimeError(f"Failed to decode {src_path}")
    src_h, src_w = src_img.shape[:2]
    print(f"  source resolution: {src_w}x{src_h}")

    fov_h, fov_v = get_assumed_fov(src_w, src_h, canvas_fov_deg)
    print(f"  assumed FOV: {fov_h:.2f} x {fov_v:.2f} deg "
          f"(whole photo width = {canvas_fov_deg}\u00b0 virtual screen; height from aspect ratio)")

    canvas_size = compute_canvas_size(fov_h, fov_v, src_w, src_h, canvas_fov_deg)
    print(f"  canvas: {canvas_size}x{canvas_size} px (covers {canvas_fov_deg} deg)")

    map_x, map_y = build_equirect_map(fov_h, src_w, src_h, canvas_size, canvas_fov_deg)
    eq_img = cv2.remap(src_img, map_x, map_y, interpolation=interp,
                        borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))

    encode_params = [
        cv2.IMWRITE_JPEG_QUALITY, quality,
        cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444,  # no chroma subsampling
        cv2.IMWRITE_JPEG_OPTIMIZE, 1,
    ]
    ok, enc = cv2.imencode('.jpg', eq_img, encode_params)
    if not ok:
        raise RuntimeError(f"JPEG encoding failed for {src_path}")

    out_bytes = strip_leading_app0(enc.tobytes())

    exif_seg = extract_exif_app1(raw)
    if exif_seg:
        try:
            exif_seg = patch_exif_dimensions(exif_seg, canvas_size, canvas_size)
            print("  EXIF: carried over from source, dimensions patched")
        except Exception as e:
            print(f"  [warn] EXIF dimension patch failed ({e}); keeping original EXIF as-is")
    else:
        print("  [warn] no Exif block found in source; output will have no EXIF")

    out_bytes = insert_exif(out_bytes, exif_seg)

    out_path = os.path.join(out_dir, f"{name}_{int(canvas_fov_deg)}deg.jpg")
    with open(out_path, 'wb') as f:
        f.write(out_bytes)
    print(f"  -> wrote {out_path} "
          f"({os.path.getsize(out_path):,} bytes, source was {os.path.getsize(src_path):,} bytes)")
    return out_path


# ============================================================
# 5. CLI
# ============================================================
def main():
    ap = argparse.ArgumentParser(
        description="Batch-convert regular single-lens JPEG photos to narrow-FOV equirectangular JPEGs for VR180 Creator.")
    ap.add_argument('--src', default='.', help="Folder containing .jpg/.jpeg files (default: current folder)")
    ap.add_argument('--out', default='./equirect_out', help="Output folder (default: ./equirect_out)")
    ap.add_argument('--fov-canvas', type=float, default=60.0,
                     help="Virtual screen width in degrees -- the whole photo's width is assumed to span "
                          "this many degrees; must match the FOV you set in VR180 Creator (default: 60)")
    ap.add_argument('--quality', type=int, default=100, help="Output JPEG quality 1-100 (default: 100)")
    ap.add_argument('--fast', action='store_true',
                     help="Use bilinear interpolation instead of Lanczos4 (faster, slightly softer)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    interp = cv2.INTER_LINEAR if args.fast else cv2.INTER_LANCZOS4

    files = sorted(set(
        glob.glob(os.path.join(args.src, '*.jpg')) +
        glob.glob(os.path.join(args.src, '*.JPG')) +
        glob.glob(os.path.join(args.src, '*.jpeg')) +
        glob.glob(os.path.join(args.src, '*.JPEG'))
    ))
    print(f"found {len(files)} JPEG file(s) in {args.src}")

    ok, failed = [], []
    for p in files:
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
