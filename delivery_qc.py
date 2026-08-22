#!/usr/bin/env python3
"""Read-only delivery QC for finished MP4/MOV files.

The tool never writes beside or into input media paths. It creates one JSON
report and one Markdown report in the requested output directory.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


MEDIA_SUFFIXES = {".mp4", ".mov"}
ISSUE_RANK = {"info": 0, "warn": 1, "fail": 2}
DEFAULT_SPEC: dict[str, Any] = {
    "require_audio": True,
    "allowed_extensions": [".mp4", ".mov"],
    "mp4_video_codecs": ["h264", "hevc"],
    "mp4_audio_codecs": ["aac", "alac", "mp3"],
    "mov_video_codecs": ["h264", "hevc", "prores"],
    "mov_audio_codecs": ["aac", "alac", "pcm_s16le", "pcm_s24le", "pcm_s32le"],
    "max_black_segment_sec": 3.0,
    "max_freeze_segment_sec": 3.0,
    "black_detect_min_duration_sec": 0.5,
    "freeze_detect_min_duration_sec": 2.0,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only pre-delivery QC for finished MP4/MOV outputs."
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="Input MP4/MOV files or directories to scan recursively.",
    )
    parser.add_argument(
        "--scan-root",
        help="Recursively scan this root for .mp4 and .mov files.",
    )
    parser.add_argument(
        "--out-dir",
        default="delivery_qc_reports",
        help="Directory where JSON and Markdown reports will be written.",
    )
    parser.add_argument(
        "--spec",
        help="Optional JSON delivery spec with duration, resolution, codec, or loudness limits.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Timeout in seconds for each ffmpeg/ffprobe command. Default: 900.",
    )
    parser.add_argument(
        "--fail-on",
        choices=["fail", "warn", "never"],
        default="fail",
        help="Process exit behavior. Default exits non-zero only when a file fails.",
    )
    return parser.parse_args()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def display_duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "unknown"
    seconds = max(0.0, seconds)
    whole = int(seconds)
    ms = int(round((seconds - whole) * 1000))
    if ms == 1000:
        whole += 1
        ms = 0
    hours, rem = divmod(whole, 3600)
    minutes, sec = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}.{ms:03d}"


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)
        return None
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number


def parse_ratio(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).strip()
    if "/" in text:
        left, right = text.split("/", 1)
        numerator = to_float(left)
        denominator = to_float(right)
        if numerator is None or denominator in (None, 0.0):
            return None
        return numerator / denominator
    return to_float(text)


def parse_ffmpeg_duration(text: str) -> float | None:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def clean_codec(codec: str | None) -> str | None:
    if not codec:
        return None
    codec = codec.strip().lower()
    codec = codec.split("(", 1)[0].strip()
    codec = codec.replace(" ", "_")
    return codec or None


def tail_lines(text: str, max_lines: int = 80) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-max_lines:]


def add_issue(
    result: dict[str, Any],
    severity: str,
    check: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    result.setdefault("issues", []).append(
        {
            "severity": severity,
            "check": check,
            "message": message,
            "details": details or {},
        }
    )


def result_status(issues: list[dict[str, Any]]) -> str:
    worst = 0
    for issue in issues:
        worst = max(worst, ISSUE_RANK.get(issue.get("severity", "info"), 0))
    if worst >= ISSUE_RANK["fail"]:
        return "fail"
    if worst >= ISSUE_RANK["warn"]:
        return "warn"
    return "pass"


def run_command(command: list[str], timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
            "elapsed_sec": round(time.monotonic() - started, 3),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return {
            "command": command,
            "returncode": None,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
            "elapsed_sec": round(time.monotonic() - started, 3),
        }
    except OSError as exc:
        return {
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "timed_out": False,
            "elapsed_sec": round(time.monotonic() - started, 3),
            "os_error": True,
        }


def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return str(Path(found))
    if not name.lower().endswith(".exe"):
        found = shutil.which(f"{name}.exe")
        if found:
            return str(Path(found))
    return None


def find_ffprobe(ffmpeg_path: str | None) -> str | None:
    ffprobe = find_tool("ffprobe")
    if ffprobe:
        return ffprobe
    if not ffmpeg_path:
        return None
    ffmpeg = Path(ffmpeg_path)
    candidates = [
        ffmpeg.with_name("ffprobe.exe"),
        ffmpeg.with_name("ffprobe"),
        ffmpeg.parent.parent / "bin" / "ffprobe.exe",
        ffmpeg.parent.parent / "bin" / "ffprobe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def load_spec(path: str | None) -> dict[str, Any]:
    spec = dict(DEFAULT_SPEC)
    if not path:
        return spec
    spec_path = Path(path)
    with spec_path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError("Spec JSON must be an object.")
    spec.update(loaded)
    return spec


def discover_inputs(inputs: list[str], scan_root: str | None) -> list[Path]:
    discovered: list[Path] = []

    def add_path(path: Path) -> None:
        if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES:
            discovered.append(path.resolve())
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.is_file() and child.suffix.lower() in MEDIA_SUFFIXES:
                    discovered.append(child.resolve())
        else:
            discovered.append(path.resolve())

    if scan_root:
        add_path(Path(scan_root))
    for raw in inputs:
        add_path(Path(raw))

    unique: list[Path] = []
    seen: set[str] = set()
    for path in discovered:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def probe_with_ffprobe(path: Path, ffprobe: str, timeout: int) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    run = run_command(command, timeout)
    if run["timed_out"] or run["returncode"] != 0:
        return None, {
            "ok": False,
            "method": "ffprobe",
            "returncode": run["returncode"],
            "timed_out": run["timed_out"],
            "stderr_tail": tail_lines(run["stderr"]),
            "elapsed_sec": run["elapsed_sec"],
        }
    try:
        parsed = json.loads(run["stdout"])
    except json.JSONDecodeError:
        return None, {
            "ok": False,
            "method": "ffprobe",
            "returncode": run["returncode"],
            "timed_out": False,
            "stderr_tail": tail_lines(run["stderr"]),
            "elapsed_sec": run["elapsed_sec"],
            "error": "ffprobe did not return valid JSON.",
        }
    return metadata_from_ffprobe(parsed), {
        "ok": True,
        "method": "ffprobe",
        "elapsed_sec": run["elapsed_sec"],
    }


def metadata_from_ffprobe(data: dict[str, Any]) -> dict[str, Any]:
    format_info = data.get("format") or {}
    streams: list[dict[str, Any]] = []
    for stream in data.get("streams") or []:
        item = {
            "index": stream.get("index"),
            "codec_type": stream.get("codec_type"),
            "codec_name": clean_codec(stream.get("codec_name")),
            "codec_long_name": stream.get("codec_long_name"),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "duration_sec": to_float(stream.get("duration")),
            "bit_rate": to_float(stream.get("bit_rate")),
            "avg_frame_rate": stream.get("avg_frame_rate"),
            "fps": parse_ratio(stream.get("avg_frame_rate") or stream.get("r_frame_rate")),
            "sample_rate": to_float(stream.get("sample_rate")),
            "channels": stream.get("channels"),
            "channel_layout": stream.get("channel_layout"),
            "tags": stream.get("tags") or {},
            "disposition": stream.get("disposition") or {},
        }
        streams.append(item)

    duration = to_float(format_info.get("duration"))
    if duration is None:
        durations = [to_float(stream.get("duration_sec")) for stream in streams]
        duration_values = [value for value in durations if value is not None]
        duration = max(duration_values) if duration_values else None

    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    primary_video = video_streams[0] if video_streams else {}

    return {
        "source": "ffprobe",
        "format": {
            "format_name": format_info.get("format_name"),
            "format_long_name": format_info.get("format_long_name"),
            "duration_sec": duration,
            "size_bytes": to_float(format_info.get("size")),
            "bit_rate": to_float(format_info.get("bit_rate")),
            "tags": format_info.get("tags") or {},
        },
        "duration_sec": duration,
        "width": primary_video.get("width"),
        "height": primary_video.get("height"),
        "fps": primary_video.get("fps"),
        "streams": streams,
        "video_streams": video_streams,
        "audio_streams": audio_streams,
        "subtitle_streams": subtitle_streams,
    }


def probe_with_ffmpeg(path: Path, ffmpeg: str, timeout: int) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    command = [ffmpeg, "-hide_banner", "-i", str(path)]
    run = run_command(command, min(timeout, 60))
    stderr = run["stderr"]
    metadata = metadata_from_ffmpeg_stderr(stderr)
    if not metadata:
        return None, {
            "ok": False,
            "method": "ffmpeg_stderr",
            "returncode": run["returncode"],
            "timed_out": run["timed_out"],
            "stderr_tail": tail_lines(stderr),
            "elapsed_sec": run["elapsed_sec"],
        }
    return metadata, {
        "ok": True,
        "method": "ffmpeg_stderr",
        "returncode": run["returncode"],
        "elapsed_sec": run["elapsed_sec"],
    }


def metadata_from_ffmpeg_stderr(stderr: str) -> dict[str, Any] | None:
    duration = parse_ffmpeg_duration(stderr)
    format_name = None
    bit_rate: float | None = None
    streams: list[dict[str, Any]] = []

    for line in stderr.splitlines():
        stripped = line.strip()
        format_match = re.search(r"Input #\d+,\s*(.+?),\s*from\s", stripped)
        if format_match:
            format_name = format_match.group(1).strip()
        bitrate_match = re.search(r"bitrate:\s*([0-9.]+)\s*kb/s", stripped)
        if bitrate_match:
            bit_rate = float(bitrate_match.group(1)) * 1000
        stream_match = re.search(
            r"Stream #(?P<index>\d+:\d+)(?:\[[^\]]+\])?(?:\([^)]+\))?:\s*"
            r"(?P<type>Video|Audio|Subtitle):\s*(?P<rest>.+)",
            stripped,
        )
        if not stream_match:
            continue
        stream_type = stream_match.group("type").lower()
        rest = stream_match.group("rest")
        codec = clean_codec(rest.split(",", 1)[0])
        stream: dict[str, Any] = {
            "index": stream_match.group("index"),
            "codec_type": stream_type,
            "codec_name": codec,
            "raw": rest,
        }
        if stream_type == "video":
            resolution = re.search(r"(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)", rest)
            fps = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*fps", rest)
            if resolution:
                stream["width"] = int(resolution.group(1))
                stream["height"] = int(resolution.group(2))
            if fps:
                stream["fps"] = float(fps.group(1))
        elif stream_type == "audio":
            sample_rate = re.search(r"([0-9]+)\s*Hz", rest)
            if sample_rate:
                stream["sample_rate"] = float(sample_rate.group(1))
            if "stereo" in rest.lower():
                stream["channels"] = 2
                stream["channel_layout"] = "stereo"
            elif "mono" in rest.lower():
                stream["channels"] = 1
                stream["channel_layout"] = "mono"
            channel_match = re.search(r"([0-9](?:\.[0-9])?)\s*channels?", rest, re.I)
            if channel_match and "channels" not in stream:
                stream["channels"] = to_float(channel_match.group(1))
        streams.append(stream)

    if not streams and duration is None and format_name is None:
        return None

    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    primary_video = video_streams[0] if video_streams else {}
    return {
        "source": "ffmpeg_stderr",
        "format": {
            "format_name": format_name,
            "format_long_name": None,
            "duration_sec": duration,
            "size_bytes": None,
            "bit_rate": bit_rate,
            "tags": {},
        },
        "duration_sec": duration,
        "width": primary_video.get("width"),
        "height": primary_video.get("height"),
        "fps": primary_video.get("fps"),
        "streams": streams,
        "video_streams": video_streams,
        "audio_streams": audio_streams,
        "subtitle_streams": subtitle_streams,
    }


def check_decode(path: Path, ffmpeg: str | None, timeout: int) -> dict[str, Any]:
    if not ffmpeg:
        return {"checked": False, "ok": None, "reason": "ffmpeg not found"}
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-v",
        "warning",
        "-xerror",
        "-err_detect",
        "explode",
        "-i",
        str(path),
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-f",
        "null",
        "-",
    ]
    run = run_command(command, timeout)
    lines = tail_lines(run["stderr"], 120)
    error_patterns = re.compile(
        r"(error|invalid|corrupt|damaged|decode|missing picture|non-existing|"
        r"concealing|moov atom not found|end of file)",
        re.I,
    )
    suspect = [line for line in lines if error_patterns.search(line)]
    ok = not run["timed_out"] and run["returncode"] == 0 and not suspect
    return {
        "checked": True,
        "ok": ok,
        "returncode": run["returncode"],
        "timed_out": run["timed_out"],
        "elapsed_sec": run["elapsed_sec"],
        "warning_or_error_lines": lines,
        "suspect_line_count": len(suspect),
    }


def check_loudness(
    path: Path,
    ffmpeg: str | None,
    has_audio: bool,
    timeout: int,
) -> dict[str, Any]:
    if not has_audio:
        return {"checked": False, "ok": None, "reason": "no audio stream"}
    if not ffmpeg:
        return {"checked": False, "ok": None, "reason": "ffmpeg not found"}

    ebur128_command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-v",
        "info",
        "-i",
        str(path),
        "-vn",
        "-filter_complex",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
    ]
    ebur128 = run_command(ebur128_command, timeout)
    integrated_values = [
        float(match.group(1))
        for match in re.finditer(r"\bI:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*LUFS", ebur128["stderr"])
    ]
    lra_values = [
        float(match.group(1))
        for match in re.finditer(r"\bLRA:\s*([0-9]+(?:\.[0-9]+)?)\s*LU", ebur128["stderr"])
    ]
    if not ebur128["timed_out"] and integrated_values:
        return {
            "checked": True,
            "ok": ebur128["returncode"] == 0,
            "method": "ebur128",
            "integrated_lufs": integrated_values[-1],
            "loudness_range_lu": lra_values[-1] if lra_values else None,
            "returncode": ebur128["returncode"],
            "timed_out": ebur128["timed_out"],
            "elapsed_sec": ebur128["elapsed_sec"],
        }

    volumedetect_command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-v",
        "info",
        "-i",
        str(path),
        "-vn",
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    volume = run_command(volumedetect_command, timeout)
    mean_match = re.search(r"mean_volume:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*dB", volume["stderr"])
    max_match = re.search(r"max_volume:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*dB", volume["stderr"])
    return {
        "checked": bool(mean_match or max_match),
        "ok": volume["returncode"] == 0 and bool(mean_match or max_match),
        "method": "volumedetect",
        "integrated_lufs": None,
        "mean_volume_db": float(mean_match.group(1)) if mean_match else None,
        "max_volume_db": float(max_match.group(1)) if max_match else None,
        "returncode": volume["returncode"],
        "timed_out": volume["timed_out"],
        "elapsed_sec": volume["elapsed_sec"],
        "fallback_reason": "ebur128 did not produce integrated LUFS",
        "ebur128_stderr_tail": tail_lines(ebur128["stderr"], 40),
    }


def check_visual_anomalies(
    path: Path,
    ffmpeg: str | None,
    has_video: bool,
    spec: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    if not has_video:
        return {"checked": False, "ok": None, "reason": "no video stream"}
    if not ffmpeg:
        return {"checked": False, "ok": None, "reason": "ffmpeg not found"}

    black_duration = to_float(spec.get("black_detect_min_duration_sec")) or 0.5
    freeze_duration = to_float(spec.get("freeze_detect_min_duration_sec")) or 2.0
    filtergraph = (
        f"blackdetect=d={black_duration}:pic_th=0.98,"
        f"freezedetect=n=-60dB:d={freeze_duration}"
    )
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-v",
        "info",
        "-i",
        str(path),
        "-an",
        "-vf",
        filtergraph,
        "-f",
        "null",
        "-",
    ]
    run = run_command(command, timeout)
    stderr = run["stderr"]

    black_segments = []
    for match in re.finditer(
        r"black_start:(-?[0-9]+(?:\.[0-9]+)?)\s+"
        r"black_end:(-?[0-9]+(?:\.[0-9]+)?)\s+"
        r"black_duration:([0-9]+(?:\.[0-9]+)?)",
        stderr,
    ):
        black_segments.append(
            {
                "start_sec": float(match.group(1)),
                "end_sec": float(match.group(2)),
                "duration_sec": float(match.group(3)),
            }
        )

    freeze_segments = []
    current: dict[str, float] = {}
    for line in stderr.splitlines():
        start = re.search(r"lavfi\.freezedetect\.freeze_start:\s*(-?[0-9]+(?:\.[0-9]+)?)", line)
        duration = re.search(r"lavfi\.freezedetect\.freeze_duration:\s*([0-9]+(?:\.[0-9]+)?)", line)
        end = re.search(r"lavfi\.freezedetect\.freeze_end:\s*(-?[0-9]+(?:\.[0-9]+)?)", line)
        if start:
            current = {"start_sec": float(start.group(1))}
        if duration:
            current["duration_sec"] = float(duration.group(1))
        if end:
            current["end_sec"] = float(end.group(1))
            if "start_sec" not in current and "duration_sec" in current:
                current["start_sec"] = current["end_sec"] - current["duration_sec"]
            if "duration_sec" not in current and "start_sec" in current:
                current["duration_sec"] = current["end_sec"] - current["start_sec"]
            freeze_segments.append(dict(current))
            current = {}

    return {
        "checked": True,
        "ok": not run["timed_out"] and run["returncode"] == 0,
        "returncode": run["returncode"],
        "timed_out": run["timed_out"],
        "elapsed_sec": run["elapsed_sec"],
        "black_segments": black_segments,
        "freeze_segments": freeze_segments,
        "stderr_tail": tail_lines(stderr, 60) if run["returncode"] != 0 or run["timed_out"] else [],
    }


def check_subtitle_timing(
    path: Path,
    ffprobe: str | None,
    subtitle_streams: list[dict[str, Any]],
    duration_sec: float | None,
    timeout: int,
) -> dict[str, Any]:
    if not subtitle_streams:
        return {"checked": True, "present": False, "stream_count": 0}
    if not ffprobe:
        return {
            "checked": False,
            "present": True,
            "stream_count": len(subtitle_streams),
            "reason": "ffprobe not found; packet timing requires ffprobe",
        }

    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "s",
        "-show_packets",
        "-show_entries",
        "packet=stream_index,pts_time,duration_time",
        "-print_format",
        "json",
        str(path),
    ]
    run = run_command(command, timeout)
    if run["timed_out"] or run["returncode"] != 0:
        return {
            "checked": False,
            "present": True,
            "stream_count": len(subtitle_streams),
            "returncode": run["returncode"],
            "timed_out": run["timed_out"],
            "stderr_tail": tail_lines(run["stderr"]),
        }
    try:
        packets = json.loads(run["stdout"]).get("packets") or []
    except json.JSONDecodeError:
        return {
            "checked": False,
            "present": True,
            "stream_count": len(subtitle_streams),
            "reason": "ffprobe subtitle packet output was not valid JSON",
        }

    first_pts: float | None = None
    last_end: float | None = None
    negative_timestamps = 0
    negative_durations = 0
    beyond_duration = 0
    for packet in packets:
        pts = to_float(packet.get("pts_time"))
        packet_duration = to_float(packet.get("duration_time")) or 0.0
        if pts is None:
            continue
        if first_pts is None or pts < first_pts:
            first_pts = pts
        end = pts + packet_duration
        if last_end is None or end > last_end:
            last_end = end
        if pts < -0.001:
            negative_timestamps += 1
        if packet_duration < -0.001:
            negative_durations += 1
        if duration_sec is not None and end > duration_sec + 1.0:
            beyond_duration += 1

    return {
        "checked": True,
        "present": True,
        "stream_count": len(subtitle_streams),
        "packet_count": len(packets),
        "first_pts_sec": first_pts,
        "last_end_sec": last_end,
        "negative_timestamps": negative_timestamps,
        "negative_durations": negative_durations,
        "packets_beyond_duration": beyond_duration,
        "elapsed_sec": run["elapsed_sec"],
    }


def spec_number(spec: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in spec:
            return to_float(spec.get(key))
    return None


def spec_list(spec: dict[str, Any], key: str) -> list[str]:
    value = spec.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [value.lower()]
    if isinstance(value, list):
        return [str(item).lower() for item in value]
    return []


def check_basic_and_spec(result: dict[str, Any], spec: dict[str, Any]) -> None:
    metadata = result.get("metadata") or {}
    duration = metadata.get("duration_sec")
    width = metadata.get("width")
    height = metadata.get("height")
    video_streams = metadata.get("video_streams") or []
    audio_streams = metadata.get("audio_streams") or []
    subtitle_streams = metadata.get("subtitle_streams") or []
    suffix = Path(result["path"]).suffix.lower()

    allowed_extensions = spec_list(spec, "allowed_extensions") or [".mp4", ".mov"]
    if suffix not in allowed_extensions:
        add_issue(
            result,
            "fail",
            "container",
            f"File extension {suffix or '(none)'} is not in allowed extensions.",
            {"allowed_extensions": allowed_extensions},
        )

    if not video_streams:
        add_issue(result, "fail", "video", "No video stream was detected.")
    if spec.get("require_audio", True) and not audio_streams:
        add_issue(result, "fail", "audio", "No audio stream was detected.")

    if duration is None:
        add_issue(result, "fail", "duration", "Duration could not be determined.")
    elif duration <= 0:
        add_issue(result, "fail", "duration", "Duration is zero or negative.")

    if video_streams and (width is None or height is None):
        add_issue(result, "warn", "resolution", "Video resolution could not be determined.")

    primary_video = video_streams[0] if video_streams else {}
    primary_audio = audio_streams[0] if audio_streams else {}
    video_codec = primary_video.get("codec_name")
    audio_codec = primary_audio.get("codec_name")
    if suffix == ".mp4":
        allowed_video = spec_list(spec, "mp4_video_codecs")
        allowed_audio = spec_list(spec, "mp4_audio_codecs")
    elif suffix == ".mov":
        allowed_video = spec_list(spec, "mov_video_codecs")
        allowed_audio = spec_list(spec, "mov_audio_codecs")
    else:
        allowed_video = []
        allowed_audio = []
    if video_codec and allowed_video and video_codec.lower() not in allowed_video:
        add_issue(
            result,
            "warn",
            "codec_compliance",
            f"Video codec {video_codec} is outside the default delivery allow-list.",
            {"allowed": allowed_video},
        )
    if audio_codec and allowed_audio and audio_codec.lower() not in allowed_audio:
        add_issue(
            result,
            "warn",
            "codec_compliance",
            f"Audio codec {audio_codec} is outside the default delivery allow-list.",
            {"allowed": allowed_audio},
        )

    expected_duration = spec_number(spec, "expected_duration_sec", "duration_sec")
    tolerance = spec_number(spec, "duration_tolerance_sec", "tolerance_sec") or 0.5
    if expected_duration is not None and duration is not None:
        delta = abs(duration - expected_duration)
        if delta > tolerance:
            add_issue(
                result,
                "fail",
                "duration_spec",
                "Duration differs from expected spec.",
                {"expected_sec": expected_duration, "actual_sec": duration, "delta_sec": delta, "tolerance_sec": tolerance},
            )
    min_duration = spec_number(spec, "min_duration_sec", "min_duration")
    max_duration = spec_number(spec, "max_duration_sec", "max_duration")
    if min_duration is not None and duration is not None and duration < min_duration:
        add_issue(
            result,
            "fail",
            "duration_spec",
            "Duration is shorter than the minimum spec.",
            {"min_duration_sec": min_duration, "actual_sec": duration},
        )
    if max_duration is not None and duration is not None and duration > max_duration:
        add_issue(
            result,
            "fail",
            "duration_spec",
            "Duration is longer than the maximum spec.",
            {"max_duration_sec": max_duration, "actual_sec": duration},
        )

    expected_width = spec_number(spec, "width")
    expected_height = spec_number(spec, "height")
    if expected_width is not None and width is not None and int(width) != int(expected_width):
        add_issue(
            result,
            "fail",
            "resolution_spec",
            "Width differs from expected spec.",
            {"expected_width": int(expected_width), "actual_width": width},
        )
    if expected_height is not None and height is not None and int(height) != int(expected_height):
        add_issue(
            result,
            "fail",
            "resolution_spec",
            "Height differs from expected spec.",
            {"expected_height": int(expected_height), "actual_height": height},
        )
    min_width = spec_number(spec, "min_width")
    min_height = spec_number(spec, "min_height")
    if min_width is not None and width is not None and width < min_width:
        add_issue(result, "fail", "resolution_spec", "Width is below minimum spec.", {"min_width": min_width, "actual_width": width})
    if min_height is not None and height is not None and height < min_height:
        add_issue(result, "fail", "resolution_spec", "Height is below minimum spec.", {"min_height": min_height, "actual_height": height})

    if subtitle_streams:
        add_issue(
            result,
            "info",
            "subtitles",
            "Subtitle stream(s) detected; timing basics will be checked when ffprobe is available.",
            {"stream_count": len(subtitle_streams)},
        )


def apply_check_results(result: dict[str, Any], spec: dict[str, Any]) -> None:
    decode = result.get("checks", {}).get("decode") or {}
    if decode.get("checked") is False:
        add_issue(result, "warn", "decode", f"Decode check skipped: {decode.get('reason')}")
    elif decode.get("timed_out"):
        add_issue(result, "fail", "decode", "Decode check timed out.", {"elapsed_sec": decode.get("elapsed_sec")})
    elif decode.get("ok") is False:
        add_issue(
            result,
            "fail",
            "decode",
            "ffmpeg reported decode warnings or errors.",
            {"returncode": decode.get("returncode"), "lines": decode.get("warning_or_error_lines", [])[:20]},
        )

    loudness = result.get("checks", {}).get("loudness") or {}
    if loudness.get("checked") is False and loudness.get("reason") != "no audio stream":
        add_issue(result, "warn", "loudness", f"Loudness check skipped: {loudness.get('reason')}")
    elif loudness.get("checked") and loudness.get("method") == "volumedetect":
        add_issue(
            result,
            "warn",
            "loudness",
            "Integrated LUFS was unavailable; volumedetect dBFS values were recorded as a fallback.",
        )
    elif loudness.get("checked") and loudness.get("ok") is False:
        add_issue(result, "warn", "loudness", "Loudness command finished with a non-zero exit code.")

    lufs = loudness.get("integrated_lufs")
    lufs_min = spec_number(spec, "lufs_min", "integrated_lufs_min")
    lufs_max = spec_number(spec, "lufs_max", "integrated_lufs_max")
    if lufs is not None and lufs_min is not None and lufs < lufs_min:
        add_issue(result, "warn", "loudness_spec", "Integrated loudness is below spec range.", {"lufs_min": lufs_min, "actual_lufs": lufs})
    if lufs is not None and lufs_max is not None and lufs > lufs_max:
        add_issue(result, "warn", "loudness_spec", "Integrated loudness is above spec range.", {"lufs_max": lufs_max, "actual_lufs": lufs})

    visual = result.get("checks", {}).get("visual") or {}
    if visual.get("checked") is False:
        add_issue(result, "warn", "visual", f"Black/freeze frame checks skipped: {visual.get('reason')}")
    elif visual.get("timed_out"):
        add_issue(result, "warn", "visual", "Black/freeze frame checks timed out.", {"elapsed_sec": visual.get("elapsed_sec")})
    elif visual.get("ok") is False:
        add_issue(result, "warn", "visual", "Black/freeze frame command returned a non-zero exit code.", {"stderr_tail": visual.get("stderr_tail", [])})
    else:
        black_limit = spec_number(spec, "max_black_segment_sec")
        freeze_limit = spec_number(spec, "max_freeze_segment_sec")
        for segment in visual.get("black_segments") or []:
            if black_limit is not None and segment.get("duration_sec", 0) > black_limit:
                add_issue(result, "warn", "black_frames", "Long black segment detected.", segment)
        for segment in visual.get("freeze_segments") or []:
            if freeze_limit is not None and segment.get("duration_sec", 0) > freeze_limit:
                add_issue(result, "warn", "freeze_frames", "Long frozen-frame segment detected.", segment)

    subtitles = result.get("checks", {}).get("subtitles") or {}
    if subtitles.get("present") and subtitles.get("checked") is False:
        add_issue(result, "warn", "subtitle_timing", f"Subtitle timing check skipped: {subtitles.get('reason')}")
    elif subtitles.get("present") and subtitles.get("checked"):
        if subtitles.get("packet_count", 0) == 0:
            add_issue(result, "warn", "subtitle_timing", "Subtitle stream has no readable subtitle packets.")
        if subtitles.get("negative_timestamps", 0) > 0:
            add_issue(result, "warn", "subtitle_timing", "Subtitle packets with negative timestamps detected.", {"count": subtitles.get("negative_timestamps")})
        if subtitles.get("negative_durations", 0) > 0:
            add_issue(result, "warn", "subtitle_timing", "Subtitle packets with negative durations detected.", {"count": subtitles.get("negative_durations")})
        if subtitles.get("packets_beyond_duration", 0) > 0:
            add_issue(result, "warn", "subtitle_timing", "Subtitle packets extend beyond media duration.", {"count": subtitles.get("packets_beyond_duration")})


def qc_file(
    path: Path,
    tools: dict[str, str | None],
    spec: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "read_only": True,
        "size_bytes": path.stat().st_size if path.exists() else None,
        "metadata": {},
        "checks": {},
        "issues": [],
        "status": "pass",
    }
    if not path.exists():
        add_issue(result, "fail", "input", "Input path does not exist.")
        result["status"] = result_status(result["issues"])
        return result
    if not path.is_file():
        add_issue(result, "fail", "input", "Input path is not a file.")
        result["status"] = result_status(result["issues"])
        return result

    metadata = None
    probe_details: dict[str, Any] = {"ok": False, "method": None}
    if tools.get("ffprobe"):
        metadata, probe_details = probe_with_ffprobe(path, tools["ffprobe"] or "", timeout)
        if metadata is None and tools.get("ffmpeg"):
            add_issue(result, "warn", "probe", "ffprobe failed; falling back to ffmpeg stderr parsing.", probe_details)
    if metadata is None and tools.get("ffmpeg"):
        metadata, probe_details = probe_with_ffmpeg(path, tools["ffmpeg"] or "", timeout)
        if metadata:
            add_issue(result, "info", "probe", "Metadata was parsed from ffmpeg stderr because ffprobe is unavailable or failed.")
    if metadata is None:
        add_issue(result, "fail", "probe", "Could not probe media metadata.", probe_details)
        result["checks"]["probe"] = probe_details
        result["status"] = result_status(result["issues"])
        return result

    result["metadata"] = metadata
    result["checks"]["probe"] = probe_details
    check_basic_and_spec(result, spec)

    video_streams = metadata.get("video_streams") or []
    audio_streams = metadata.get("audio_streams") or []
    subtitle_streams = metadata.get("subtitle_streams") or []

    result["checks"]["decode"] = check_decode(path, tools.get("ffmpeg"), timeout)
    result["checks"]["loudness"] = check_loudness(path, tools.get("ffmpeg"), bool(audio_streams), timeout)
    result["checks"]["visual"] = check_visual_anomalies(path, tools.get("ffmpeg"), bool(video_streams), spec, timeout)
    result["checks"]["subtitles"] = check_subtitle_timing(
        path,
        tools.get("ffprobe"),
        subtitle_streams,
        metadata.get("duration_sec"),
        timeout,
    )
    apply_check_results(result, spec)
    result["status"] = result_status(result["issues"])
    return result


def status_badge(status: str) -> str:
    return {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}.get(status, status.upper())


def stream_summary(streams: list[dict[str, Any]], stream_type: str) -> str:
    if not streams:
        return "none"
    stream = streams[0]
    codec = stream.get("codec_name") or "unknown"
    if stream_type == "video":
        width = stream.get("width")
        height = stream.get("height")
        fps = stream.get("fps")
        parts = [codec]
        if width and height:
            parts.append(f"{width}x{height}")
        if fps:
            parts.append(f"{fps:.3f} fps")
        if len(streams) > 1:
            parts.append(f"+{len(streams) - 1} video")
        return " ".join(parts)
    if stream_type == "audio":
        channels = stream.get("channels")
        sample_rate = stream.get("sample_rate")
        parts = [codec]
        if channels:
            parts.append(f"{channels} ch")
        if sample_rate:
            parts.append(f"{int(sample_rate)} Hz")
        if len(streams) > 1:
            parts.append(f"+{len(streams) - 1} audio")
        return " ".join(parts)
    return f"{len(streams)} stream(s)"


def markdown_escape(text: Any) -> str:
    return str(text).replace("|", "\\|")


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Delivery QC Report")
    lines.append("")
    lines.append(f"- Run timestamp: `{report['run']['timestamp_local']}`")
    lines.append(f"- Read-only mode: `{report['run']['read_only']}`")
    lines.append(f"- ffmpeg: `{report['tools'].get('ffmpeg') or 'not found'}`")
    lines.append(f"- ffprobe: `{report['tools'].get('ffprobe') or 'not found'}`")
    lines.append(f"- Files checked: `{len(report['files'])}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| File | Status | Duration | Video | Audio | Loudness | Issues |")
    lines.append("| --- | --- | ---: | --- | --- | --- | ---: |")
    for file_result in report["files"]:
        metadata = file_result.get("metadata") or {}
        loudness = file_result.get("checks", {}).get("loudness") or {}
        lufs = loudness.get("integrated_lufs")
        if lufs is not None:
            loudness_text = f"{lufs:.1f} LUFS"
        elif loudness.get("mean_volume_db") is not None:
            loudness_text = f"{loudness.get('mean_volume_db'):.1f} dB mean"
        else:
            loudness_text = "n/a"
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_escape(Path(file_result["path"]).name),
                    status_badge(file_result["status"]),
                    display_duration(metadata.get("duration_sec")),
                    markdown_escape(stream_summary(metadata.get("video_streams") or [], "video")),
                    markdown_escape(stream_summary(metadata.get("audio_streams") or [], "audio")),
                    loudness_text,
                    str(len([issue for issue in file_result.get("issues", []) if issue.get("severity") != "info"])),
                ]
            )
            + " |"
        )
    lines.append("")

    for file_result in report["files"]:
        metadata = file_result.get("metadata") or {}
        checks = file_result.get("checks") or {}
        visual = checks.get("visual") or {}
        subtitles = checks.get("subtitles") or {}
        loudness = checks.get("loudness") or {}
        lines.append(f"## {Path(file_result['path']).name}")
        lines.append("")
        lines.append(f"- Status: `{status_badge(file_result['status'])}`")
        lines.append(f"- Path: `{file_result['path']}`")
        lines.append(f"- Size: `{file_result.get('size_bytes')}` bytes")
        lines.append(f"- Probe source: `{metadata.get('source', 'unknown')}`")
        lines.append(f"- Container: `{(metadata.get('format') or {}).get('format_name') or 'unknown'}`")
        lines.append(f"- Duration: `{display_duration(metadata.get('duration_sec'))}`")
        lines.append(f"- Video: `{stream_summary(metadata.get('video_streams') or [], 'video')}`")
        lines.append(f"- Audio: `{stream_summary(metadata.get('audio_streams') or [], 'audio')}`")
        if loudness.get("integrated_lufs") is not None:
            lines.append(f"- Integrated loudness: `{loudness['integrated_lufs']:.2f} LUFS` via `{loudness.get('method')}`")
        elif loudness.get("mean_volume_db") is not None:
            lines.append(
                f"- Loudness fallback: mean `{loudness['mean_volume_db']:.2f} dBFS`, "
                f"max `{loudness.get('max_volume_db')}` dBFS via `volumedetect`"
            )
        else:
            lines.append(f"- Loudness: `{loudness.get('reason', 'not available')}`")
        lines.append(
            f"- Black segments: `{len(visual.get('black_segments') or [])}`; "
            f"freeze segments: `{len(visual.get('freeze_segments') or [])}`"
        )
        if subtitles.get("present"):
            lines.append(
                f"- Subtitles: `{subtitles.get('stream_count')}` stream(s), "
                f"packets `{subtitles.get('packet_count', 'unchecked')}`"
            )
        else:
            lines.append("- Subtitles: `none detected`")
        issues = file_result.get("issues") or []
        if issues:
            lines.append("")
            lines.append("| Severity | Check | Message |")
            lines.append("| --- | --- | --- |")
            for issue in issues:
                lines.append(
                    f"| {markdown_escape(issue.get('severity'))} | "
                    f"{markdown_escape(issue.get('check'))} | "
                    f"{markdown_escape(issue.get('message'))} |"
                )
        else:
            lines.append("")
            lines.append("No issues detected.")
        lines.append("")
    return "\n".join(lines)


def write_reports(report: dict[str, Any], out_dir: Path, stamp: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"delivery_qc_{stamp}.json"
    markdown_path = out_dir / f"delivery_qc_{stamp}.md"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    with markdown_path.open("w", encoding="utf-8") as handle:
        handle.write(render_markdown(report))
        handle.write("\n")
    return json_path, markdown_path


def build_report(args: argparse.Namespace) -> tuple[dict[str, Any], Path, Path]:
    stamp = now_stamp()
    spec = load_spec(args.spec)
    inputs = discover_inputs(args.inputs, args.scan_root)
    if not inputs:
        raise ValueError("No input media found. Provide input paths or --scan-root.")

    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_ffprobe(ffmpeg)
    tools = {"ffmpeg": ffmpeg, "ffprobe": ffprobe}

    report: dict[str, Any] = {
        "run": {
            "timestamp_local": datetime.now().isoformat(timespec="seconds"),
            "read_only": True,
            "input_count": len(inputs),
            "scan_root": str(Path(args.scan_root).resolve()) if args.scan_root else None,
            "spec_path": str(Path(args.spec).resolve()) if args.spec else None,
            "timeout_sec": args.timeout,
        },
        "tools": tools,
        "spec": spec,
        "files": [],
        "summary": {},
    }

    if not ffmpeg and not ffprobe:
        report["summary"]["tool_warning"] = "Neither ffmpeg nor ffprobe was found; checks will fail or be skipped."

    for path in inputs:
        report["files"].append(qc_file(path, tools, spec, args.timeout))

    counts = {"pass": 0, "warn": 0, "fail": 0}
    for file_result in report["files"]:
        counts[file_result["status"]] = counts.get(file_result["status"], 0) + 1
    report["summary"].update(counts)

    json_path, markdown_path = write_reports(report, Path(args.out_dir), stamp)
    return report, json_path, markdown_path


def exit_code_for(report: dict[str, Any], fail_on: str) -> int:
    statuses = [file_result.get("status") for file_result in report.get("files", [])]
    if fail_on == "never":
        return 0
    if fail_on == "warn" and any(status in {"warn", "fail"} for status in statuses):
        return 1
    if fail_on == "fail" and any(status == "fail" for status in statuses):
        return 2
    return 0


def main() -> int:
    args = parse_args()
    try:
        report, json_path, markdown_path = build_report(args)
    except Exception as exc:
        print(f"delivery_qc error: {exc}", file=sys.stderr)
        return 2

    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    print(
        "Summary: "
        f"pass={report['summary'].get('pass', 0)} "
        f"warn={report['summary'].get('warn', 0)} "
        f"fail={report['summary'].get('fail', 0)}"
    )
    return exit_code_for(report, args.fail_on)


if __name__ == "__main__":
    raise SystemExit(main())
