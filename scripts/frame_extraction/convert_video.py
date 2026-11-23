#!/usr/bin/env python3
"""Extract reconstruction-friendly frames from a video.

Visual mode keeps sharp frames when the viewpoint or scene content changes.
Interval mode preserves the original fixed-time extraction behavior.
"""

import argparse
import csv
import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}


@dataclass
class Candidate:
    frame_index: int
    timestamp: float
    frame: np.ndarray
    gray: np.ndarray
    keypoints: list
    descriptors: np.ndarray | None
    sharpness: float
    motion: float
    histogram_change: float
    visual_score: float


def parse_args():
    parser = argparse.ArgumentParser(
        description="从视频中选择适合COLMAP重建的清晰关键帧。默认按视觉变化选帧。"
    )
    parser.add_argument("--input", required=True, help="输入视频路径")
    parser.add_argument("--output", required=True, help="输出图片目录")
    parser.add_argument(
        "--mode", choices=("visual", "interval"), default="visual",
        help="visual按清晰度和视角变化选帧；interval按固定时间抽帧",
    )
    parser.add_argument("--scale", type=float, default=1.0, help="保存分辨率缩放除数")
    parser.add_argument("--t", type=float, default=1.0, help="interval模式的抽帧间隔秒数")
    parser.add_argument("--analysis_fps", type=float, default=10.0, help="visual模式每秒分析帧数")
    parser.add_argument("--analysis_width", type=int, default=640, help="视觉分析图像宽度")
    parser.add_argument("--min_interval", type=float, default=0.20, help="两张选中帧的最短间隔")
    parser.add_argument("--max_interval", type=float, default=1.50, help="视觉变化不足时强制保留帧的最长间隔")
    parser.add_argument("--motion_threshold", type=float, default=10.0, help="特征点中位移动阈值")
    parser.add_argument("--hist_threshold", type=float, default=0.10, help="灰度直方图变化阈值")
    parser.add_argument("--blur_threshold", type=float, default=55.0, help="拉普拉斯清晰度最低阈值")
    parser.add_argument("--max_frames", type=int, default=0, help="最多输出帧数，0表示不限制")
    parser.add_argument("--start_time", type=float, default=0.0, help="开始时间，秒")
    parser.add_argument("--end_time", type=float, default=0.0, help="结束时间，秒；0表示视频结尾")
    parser.add_argument("--jpeg_quality", type=int, default=95, help="JPEG质量，1到100")
    parser.add_argument("--show_image", type=int, choices=(0, 1), default=0, help="实时显示结果")
    parser.add_argument("--overwrite", action="store_true", help="清理输出目录中的旧图片和清单")
    parser.add_argument("--manifest", default="", help="JSON清单路径；默认写入输出目录frames.json")
    return parser.parse_args()


def validate_args(args):
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"输入视频不存在: {input_path}")
    if args.scale <= 0:
        raise ValueError("--scale必须大于0")
    if args.t <= 0 or args.analysis_fps <= 0:
        raise ValueError("--t和--analysis_fps必须大于0")
    if args.min_interval < 0 or args.max_interval <= 0:
        raise ValueError("抽帧间隔参数无效")
    if args.max_interval < args.min_interval:
        raise ValueError("--max_interval不能小于--min_interval")
    if args.end_time and args.end_time <= args.start_time:
        raise ValueError("--end_time必须大于--start_time")
    if not 1 <= args.jpeg_quality <= 100:
        raise ValueError("--jpeg_quality必须在1到100之间")
    return input_path


def prepare_output(output_dir, manifest_path, overwrite):
    output_dir.mkdir(parents=True, exist_ok=True)
    old_images = [path for path in output_dir.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES]
    old_manifests = [path for path in (manifest_path, output_dir / "frames.csv") if path.exists()]
    if old_images or old_manifests:
        if not overwrite:
            raise FileExistsError(f"输出目录已有抽帧结果，使用--overwrite覆盖: {output_dir}")
        for path in old_images + old_manifests:
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)


def analysis_image(frame, width):
    if frame.shape[1] > width:
        ratio = width / frame.shape[1]
        frame = cv2.resize(
            frame, (width, max(1, round(frame.shape[0] * ratio))),
            interpolation=cv2.INTER_AREA,
        )
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def detect_features(gray, orb):
    keypoints, descriptors = orb.detectAndCompute(gray, None)
    return keypoints or [], descriptors


def histogram(gray):
    values = cv2.calcHist([gray], [0], None, [64], [0, 256])
    cv2.normalize(values, values)
    return values


def compare_visual(previous, gray, keypoints, descriptors, matcher, motion_threshold, hist_threshold):
    motion = 0.0
    if previous.descriptors is not None and descriptors is not None:
        matches = matcher.match(previous.descriptors, descriptors)
        if matches:
            matches = sorted(matches, key=lambda match: match.distance)
            reliable = matches[: min(200, max(12, len(matches) // 2))]
            displacements = [
                math.dist(previous.keypoints[match.queryIdx].pt, keypoints[match.trainIdx].pt)
                for match in reliable
            ]
            if displacements:
                motion = float(np.median(displacements))
    histogram_change = float(
        cv2.compareHist(histogram(previous.gray), histogram(gray), cv2.HISTCMP_BHATTACHARYYA)
    )
    visual_score = max(
        motion / max(motion_threshold, 1e-6),
        histogram_change / max(hist_threshold, 1e-6),
    )
    return motion, histogram_change, visual_score


def resize_for_output(frame, scale):
    if abs(scale - 1.0) < 1e-9:
        return frame
    width = max(1, round(frame.shape[1] / scale))
    height = max(1, round(frame.shape[0] / scale))
    interpolation = cv2.INTER_AREA if scale > 1 else cv2.INTER_CUBIC
    return cv2.resize(frame, (width, height), interpolation=interpolation)


def save_candidate(candidate, output_dir, sequence, args, reason):
    filename = f"frame_{sequence:06d}.jpg"
    output_frame = resize_for_output(candidate.frame, args.scale)
    output_path = output_dir / filename
    if not cv2.imwrite(
        str(output_path), output_frame,
        [cv2.IMWRITE_JPEG_QUALITY, args.jpeg_quality],
    ):
        raise RuntimeError(f"保存图片失败: {output_path}")
    if args.show_image:
        preview = output_frame.copy()
        cv2.putText(
            preview, f"{sequence} {candidate.timestamp:.3f}s {reason}",
            (18, 32), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 255, 0), 2,
        )
        cv2.imshow("selected frame", preview)
        cv2.waitKey(1)
    return {
        "file": filename,
        "source_frame": candidate.frame_index,
        "timestamp_seconds": round(candidate.timestamp, 6),
        "sharpness": round(candidate.sharpness, 4),
        "feature_motion_pixels": round(candidate.motion, 4),
        "histogram_change": round(candidate.histogram_change, 6),
        "visual_score": round(candidate.visual_score, 4),
        "selection_reason": reason,
        "width": int(output_frame.shape[1]),
        "height": int(output_frame.shape[0]),
    }


def extract_interval(cap, args, output_dir, fps):
    records = []
    next_time = args.start_time
    frame_index = max(0, round(args.start_time * fps))
    cap.set(cv2.CAP_PROP_POS_MSEC, args.start_time * 1000)
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        timestamp = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if not math.isfinite(timestamp) or timestamp <= 0:
            timestamp = frame_index / fps
        if args.end_time and timestamp > args.end_time:
            break
        if timestamp + 1e-6 >= next_time:
            gray = analysis_image(frame, args.analysis_width)
            candidate = Candidate(
                frame_index, timestamp, frame, gray, [], None,
                float(cv2.Laplacian(gray, cv2.CV_64F).var()), 0.0, 0.0, 0.0,
            )
            records.append(save_candidate(candidate, output_dir, len(records) + 1, args, "fixed_interval"))
            next_time += args.t
            if args.max_frames and len(records) >= args.max_frames:
                break
        frame_index += 1
    return records


def extract_visual(cap, args, output_dir, fps):
    records = []
    next_analysis_time = args.start_time
    frame_index = max(0, round(args.start_time * fps))
    cap.set(cv2.CAP_PROP_POS_MSEC, args.start_time * 1000)
    orb = cv2.ORB_create(nfeatures=1800, scaleFactor=1.2, nlevels=8, fastThreshold=12)
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    previous = None
    best_pending = None
    last_selected_time = args.start_time

    def select(candidate, reason):
        nonlocal previous, best_pending, last_selected_time
        records.append(save_candidate(candidate, output_dir, len(records) + 1, args, reason))
        previous = candidate
        best_pending = None
        last_selected_time = candidate.timestamp

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        timestamp = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if not math.isfinite(timestamp) or timestamp <= 0:
            timestamp = frame_index / fps
        if args.end_time and timestamp > args.end_time:
            break
        if timestamp + 1e-6 < next_analysis_time:
            frame_index += 1
            continue
        next_analysis_time = timestamp + 1.0 / args.analysis_fps
        gray = analysis_image(frame, args.analysis_width)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        keypoints, descriptors = detect_features(gray, orb)
        if previous is None:
            motion, hist_change, score = 0.0, 0.0, 1.0
        else:
            motion, hist_change, score = compare_visual(
                previous, gray, keypoints, descriptors, matcher,
                args.motion_threshold, args.hist_threshold,
            )
        candidate = Candidate(
            frame_index, timestamp, frame.copy(), gray, keypoints, descriptors,
            sharpness, motion, hist_change, score,
        )
        elapsed = timestamp - last_selected_time
        quality = sharpness * (0.5 + min(score, 3.0))
        pending_quality = (
            -1.0 if best_pending is None
            else best_pending.sharpness * (0.5 + min(best_pending.visual_score, 3.0))
        )
        if sharpness >= args.blur_threshold and quality > pending_quality:
            best_pending = candidate
        if previous is None and sharpness >= args.blur_threshold:
            select(candidate, "first_clear_frame")
        elif elapsed >= args.min_interval and sharpness >= args.blur_threshold and score >= 1.0:
            select(candidate, "visual_change")
        elif elapsed >= args.max_interval and best_pending is not None:
            select(best_pending, "maximum_interval")
        if args.max_frames and len(records) >= args.max_frames:
            break
        frame_index += 1

    if best_pending is not None and (not args.max_frames or len(records) < args.max_frames):
        if previous is None or best_pending.timestamp - last_selected_time >= args.min_interval:
            select(best_pending, "final_clear_frame")
    return records


def extract_frames(args):
    input_path = validate_args(args)
    output_dir = Path(args.output).expanduser().resolve()
    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest else output_dir / "frames.json"
    )
    prepare_output(output_dir, manifest_path, args.overwrite)
    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {input_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    if not math.isfinite(fps) or fps <= 0:
        raise RuntimeError("无法读取有效视频帧率")
    source_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    source_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    source_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    records = (
        extract_visual(cap, args, output_dir, fps)
        if args.mode == "visual" else extract_interval(cap, args, output_dir, fps)
    )
    cap.release()
    if args.show_image:
        cv2.destroyAllWindows()
    if len(records) < 3:
        raise RuntimeError(
            f"只选出{len(records)}帧，COLMAP至少需要3帧；请降低阈值或延长视频范围"
        )
    manifest = {
        "input_video": str(input_path),
        "mode": args.mode,
        "source": {
            "fps": fps,
            "width": source_width,
            "height": source_height,
            "reported_frame_count": source_frames,
        },
        "parameters": {
            key: value for key, value in vars(args).items()
            if key not in {"input", "output", "manifest"}
        },
        "selected_frame_count": len(records),
        "frames": records,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (output_dir / "frames.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)
    print(f"视觉抽帧完成: {len(records)}张")
    print(f"图片目录: {output_dir}")
    print(f"抽帧清单: {manifest_path}")
    return manifest


if __name__ == "__main__":
    extract_frames(parse_args())
