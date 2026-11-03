"""Bounded, local-only FFmpeg processing. Publication belongs to the job service."""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import struct
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

MAX_SOURCE_BYTES = 500 * 1024 * 1024
MAX_OUTPUT_BYTES = 420 * 1024 * 1024
MAX_POSTER_BYTES = 5 * 1024 * 1024
MAX_PLAYBACK_BYTES = MAX_OUTPUT_BYTES - MAX_POSTER_BYTES
MAX_DURATION = 1200
MAX_PIXELS = 1920 * 1080
PROBE_TIMEOUT = 30
ENCODE_TIMEOUT = 1800
MAX_CAPTURE_BYTES = 64 * 1024
MAX_STDERR_BYTES = 1024 * 1024

VIDEO_CODECS = frozenset({"h264", "hevc", "vp8", "vp9", "av1", "mpeg4", "mjpeg"})
AUDIO_CODECS = frozenset(
    {"aac", "opus", "vorbis", "mp3", "flac", "pcm_s16le", "pcm_s24le"}
)
# Decoder names differ from codec names for the default AV1 and MP3 decoders.
DECODERS = ",".join(sorted(VIDEO_CODECS | AUDIO_CODECS | {"libdav1d", "mp3float"}))
FORMATS = "mov,matroska,webm"
PROBE_ENTRIES = (
    "format=format_name,duration:"
    "stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,"
    "sample_aspect_ratio,pix_fmt,channels:"
    "stream_disposition=attached_pic:stream_tags=rotate:stream_side_data=rotation"
)
INVALID_MESSAGE = (
    "Не удалось обработать видео. Проверьте целостность файла и используйте "
    "MP4, MOV или WebM с поддерживаемыми видео- и аудиокодеками."
)


class InvalidVideo(Exception):
    """Permanent input or output validation failure; safe to show to the owner."""


class TranscodeInterrupted(Exception):
    """The caller stopped work or lost its lease; never publish this attempt."""


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


@dataclass(frozen=True)
class _Input:
    info: VideoInfo
    video_index: int
    audio_index: int | None
    fps: Fraction
    target_width: int
    target_height: int


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # Reap the direct child and also kill descendants if it exited first.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _run(
    arguments: list[str],
    timeout: float,
    checkpoint: Callable[[], None],
    *,
    capture: bool = False,
) -> bytes:
    """Drain both pipes without unbounded buffers, and reap on every exit path."""
    checkpoint()
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        start_new_session=True,
        close_fds=True,
    )
    output = bytearray()
    stderr_size = 0
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    try:
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            if stream is not None:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map() or process.poll() is None:
            checkpoint()
            if time.monotonic() >= deadline:
                raise InvalidVideo("Превышено время обработки видео.")
            for key, _ in selector.select(timeout=0.25):
                data = os.read(key.fd, 64 * 1024)
                if not data:
                    selector.unregister(key.fileobj)
                elif key.data == "stdout":
                    if len(output) + len(data) > MAX_CAPTURE_BYTES:
                        raise InvalidVideo(INVALID_MESSAGE)
                    output.extend(data)
                else:
                    stderr_size += len(data)
                    if stderr_size > MAX_STDERR_BYTES:
                        raise InvalidVideo(INVALID_MESSAGE)
        if process.wait() != 0:
            # Paths and codec diagnostics are intentionally not exposed or logged.
            raise InvalidVideo(INVALID_MESSAGE)
        checkpoint()
        return bytes(output)
    finally:
        selector.close()
        _terminate(process)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _input_options() -> list[str]:
    return [
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        FORMATS,
        "-codec_whitelist",
        DECODERS,
        "-max_streams",
        "16",
        "-probesize",
        "10000000",
        "-analyzeduration",
        "10000000",
        "-threads",
        "2",
        "-max_pixels",
        str(MAX_PIXELS),
    ]


def _probe(path: Path, checkpoint: Callable[[], None]) -> dict[str, Any]:
    raw = _run(
        [
            "/usr/bin/ffprobe",
            "-hide_banner",
            "-v",
            "error",
            *_input_options(),
            "-show_entries",
            PROBE_ENTRIES,
            "-of",
            "json",
            str(path),
        ],
        PROBE_TIMEOUT,
        checkpoint,
        capture=True,
    )
    try:
        result: object = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidVideo(INVALID_MESSAGE) from exc
    if not isinstance(result, dict):
        raise InvalidVideo(INVALID_MESSAGE)
    return result


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidVideo(INVALID_MESSAGE)
    return value


def _ratio(value: object, *, separator: str = "/") -> Fraction:
    if not isinstance(value, str) or len(value) > 64:
        raise InvalidVideo(INVALID_MESSAGE)
    try:
        result = Fraction(value.replace(separator, "/"))
    except (ValueError, ZeroDivisionError) as exc:
        raise InvalidVideo(INVALID_MESSAGE) from exc
    if result <= 0:
        raise InvalidVideo(INVALID_MESSAGE)
    return result


def _fit(width: int, height: int, aspect: Fraction) -> tuple[int, int]:
    """Correct SAR while never enlarging either rotated pixel dimension."""
    max_width, max_height = (1280, 720) if aspect >= 1 else (720, 1280)
    width, height = min(width, max_width), min(height, max_height)
    if Fraction(width, height) > aspect:
        width = int(height * aspect)
    else:
        height = int(width / aspect)
    width, height = width // 2 * 2, height // 2 * 2
    if width < 2 or height < 2:
        raise InvalidVideo(INVALID_MESSAGE)
    return width, height


def _inspect(data: dict[str, Any], *, allow_unknown_duration: bool = False) -> _Input:
    try:
        container = data["format"]
        streams = data["streams"]
        if not isinstance(container, dict) or not isinstance(streams, list):
            raise InvalidVideo(INVALID_MESSAGE)
        if not 1 <= len(streams) <= 16 or not all(
            isinstance(stream, dict) for stream in streams
        ):
            raise InvalidVideo(INVALID_MESSAGE)
        formats = str(container["format_name"]).split(",")
        if not set(formats) & {"mov", "matroska", "webm"}:
            raise InvalidVideo(INVALID_MESSAGE)
        duration_value = container.get("duration")
        if allow_unknown_duration and duration_value in (None, "N/A"):
            # MediaRecorder WebM often omits duration. A bounded encode and the
            # mandatory output probe enforce the actual duration in that case.
            duration = 0.0
        else:
            if duration_value is None:
                raise InvalidVideo(INVALID_MESSAGE)
            duration = float(duration_value)
            if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
                raise InvalidVideo("Видео должно быть не длиннее 20 минут.")
        videos = [
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and not stream.get("disposition", {}).get("attached_pic", 0)
        ]
        if not videos:
            raise InvalidVideo("В файле не найден видеопоток.")
        video = videos[0]
        if video.get("codec_name") not in VIDEO_CODECS:
            raise InvalidVideo(INVALID_MESSAGE)
        width, height = _integer(video["width"]), _integer(video["height"])
        if (
            width < 2
            or height < 2
            or not (
                (width <= 1920 and height <= 1080) or (width <= 1080 and height <= 1920)
            )
        ):
            raise InvalidVideo("Максимальное разрешение — 1920×1080 или 1080×1920.")
        fps_values = [
            _ratio(video[name])
            for name in ("avg_frame_rate", "r_frame_rate")
            if video.get(name) not in (None, "0/0", "0")
        ]
        if not fps_values or max(fps_values) > Fraction(6001, 100):
            raise InvalidVideo("Допустимая частота кадров — до 60 в секунду.")
        fps = min(fps_values[0], Fraction(30))
        sar_value = video.get("sample_aspect_ratio")
        sar = (
            _ratio(sar_value, separator=":")
            if sar_value not in (None, "N/A")
            else Fraction(1)
        )
        if not Fraction(1, 16) <= sar <= 16:
            raise InvalidVideo(INVALID_MESSAGE)
        rotation = float(video.get("tags", {}).get("rotate", 0))
        for side in video.get("side_data_list", []):
            if "rotation" in side:
                rotation = float(side["rotation"])
        if (
            not math.isfinite(rotation)
            or abs(rotation / 90 - round(rotation / 90)) > 0.001
        ):
            raise InvalidVideo(INVALID_MESSAGE)
        aspect = Fraction(width, height) * sar
        if round(rotation / 90) % 2:
            width, height, aspect = height, width, 1 / aspect
        target_width, target_height = _fit(width, height, aspect)
        audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
        audio = audios[0] if audios else None
        if audio is not None and audio.get("codec_name") not in AUDIO_CODECS:
            raise InvalidVideo(INVALID_MESSAGE)
        index = _integer(video["index"])
        audio_index = _integer(audio["index"]) if audio is not None else None
        if not 0 <= index < 16 or (
            audio_index is not None and not 0 <= audio_index < 16
        ):
            raise InvalidVideo(INVALID_MESSAGE)
        return _Input(
            VideoInfo(duration, width, height, audio is not None),
            index,
            audio_index,
            fps,
            target_width,
            target_height,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise InvalidVideo(INVALID_MESSAGE) from exc


def _faststart(path: Path) -> bool:
    """Read only MP4 atom headers, not the complete video into RAM."""
    size = path.stat().st_size
    saw_moov = False
    with path.open("rb") as stream:
        for _ in range(1024):
            offset = stream.tell()
            header = stream.read(8)
            if len(header) != 8:
                return False
            atom_size, atom_type = struct.unpack(">I4s", header)
            header_size = 8
            if atom_size == 1:
                extended = stream.read(8)
                if len(extended) != 8:
                    return False
                (atom_size,) = struct.unpack(">Q", extended)
                header_size = 16
            elif atom_size == 0:
                atom_size = size - offset
            if atom_size < header_size or offset + atom_size > size:
                return False
            if atom_type == b"moov":
                saw_moov = True
            elif atom_type == b"mdat":
                return saw_moov
            stream.seek(offset + atom_size)
    return False


def _verify_output(
    path: Path, source: _Input, checkpoint: Callable[[], None]
) -> VideoInfo:
    if (
        path.is_symlink()
        or not 0 < path.stat().st_size < MAX_PLAYBACK_BYTES - 1024 * 1024
    ):
        # -fs may stop successfully at its boundary. Reject a near-limit result
        # rather than publish an otherwise valid but silently truncated video.
        raise InvalidVideo(INVALID_MESSAGE)
    data = _probe(path, checkpoint)
    output = _inspect(data)
    streams = data["streams"]
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if (
        len(videos) != 1
        or len(audios) != int(source.info.has_audio)
        or len(streams) != 1 + len(audios)
        or videos[0].get("codec_name") != "h264"
        or videos[0].get("pix_fmt") != "yuv420p"
        or videos[0].get("sample_aspect_ratio") not in ("1:1", None)
        or output.info.width != source.target_width
        or output.info.height != source.target_height
        or _ratio(videos[0]["avg_frame_rate"]) > 30
        or _ratio(videos[0]["r_frame_rate"]) > 30
        or (
            audios
            and (
                audios[0].get("codec_name") != "aac" or audios[0].get("channels", 0) > 2
            )
        )
        or (
            source.info.duration > 0
            and abs(output.info.duration - source.info.duration) > 1.5
        )
        or not _faststart(path)
    ):
        raise InvalidVideo(INVALID_MESSAGE)
    return output.info


def transcode(
    source: Path,
    work_dir: Path,
    checkpoint: Callable[[], None],
) -> VideoInfo:
    """Produce verified playback.mp4/poster.jpg in a private attempt directory.

    Exceptions leave the attempt's partial files for the caller to remove. The
    caller must still check its current lease/token before atomic publication.
    """
    checkpoint()
    if source.is_symlink() or not source.is_file():
        raise InvalidVideo(INVALID_MESSAGE)
    if not 0 < source.stat().st_size <= MAX_SOURCE_BYTES:
        raise InvalidVideo("Размер видео должен быть не больше 500 МиБ.")
    source = source.resolve(strict=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    if work_dir.is_symlink() or not work_dir.is_dir():
        raise InvalidVideo(INVALID_MESSAGE)
    work_dir = work_dir.resolve(strict=True)
    playback, poster = work_dir / "playback.mp4", work_dir / "poster.jpg"
    if (
        playback.exists()
        or playback.is_symlink()
        or poster.exists()
        or poster.is_symlink()
    ):
        raise InvalidVideo(INVALID_MESSAGE)
    video = _inspect(_probe(source, checkpoint), allow_unknown_duration=True)
    filters = (
        f"scale={video.target_width}:{video.target_height},setsar=1,"
        f"fps={video.fps.numerator}/{video.fps.denominator}"
    )
    audio_map = (
        ["-map", f"0:{video.audio_index}"] if video.audio_index is not None else []
    )
    _run(
        [
            "/usr/bin/ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-nostats",
            "-n",
            "-xerror",
            "-filter_threads",
            "2",
            *_input_options(),
            "-err_detect",
            "explode",
            "-i",
            str(source),
            "-map",
            f"0:{video.video_index}",
            *audio_map,
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-vf",
            filters,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-maxrate",
            "2500k",
            "-bufsize",
            "5000k",
            "-pix_fmt",
            "yuv420p",
            "-threads:v",
            "2",
            "-x264-params",
            "lookahead_threads=1",
            "-g",
            str(max(2, math.ceil(float(video.fps) * 2))),
            "-c:a",
            "aac",
            "-threads:a",
            "2",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-metadata:s:v:0",
            "rotate=0",
            "-movflags",
            "+faststart",
            "-fps_mode",
            "cfr",
            "-max_muxing_queue_size",
            "1024",
            "-t",
            str(MAX_DURATION + 1),
            "-fs",
            str(MAX_PLAYBACK_BYTES),
            "-f",
            "mp4",
            str(playback),
        ],
        ENCODE_TIMEOUT,
        checkpoint,
    )
    result = _verify_output(playback, video, checkpoint)
    poster_width = min(640, result.width)
    poster_height = max(2, int(poster_width * result.height / result.width) // 2 * 2)
    _run(
        [
            "/usr/bin/ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-nostats",
            "-n",
            "-xerror",
            "-filter_threads",
            "2",
            *_input_options(),
            "-i",
            str(playback),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            f"scale={poster_width}:{poster_height},setsar=1",
            "-c:v",
            "mjpeg",
            "-threads",
            "2",
            "-q:v",
            "3",
            "-update",
            "1",
            "-f",
            "image2",
            str(poster),
        ],
        PROBE_TIMEOUT,
        checkpoint,
    )
    if (
        poster.is_symlink()
        or not 4 < poster.stat().st_size <= MAX_POSTER_BYTES
        or playback.stat().st_size + poster.stat().st_size > MAX_OUTPUT_BYTES
    ):
        raise InvalidVideo(INVALID_MESSAGE)
    with poster.open("rb") as stream:
        if stream.read(2) != b"\xff\xd8":
            raise InvalidVideo(INVALID_MESSAGE)
        stream.seek(-2, os.SEEK_END)
        if stream.read(2) != b"\xff\xd9":
            raise InvalidVideo(INVALID_MESSAGE)
    # fsync before the caller renames this directory into its published location.
    for path in (playback, poster):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    checkpoint()
    return result
