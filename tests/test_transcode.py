"""Real, bounded FFmpeg and subprocess regressions; no database required."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.videos.transcode import (
    InvalidVideo,
    TranscodeInterrupted,
    _inspect,
    _run,
    transcode,
)


def _make_video(
    target: Path,
    *,
    size: str = "320x180",
    audio: bool = True,
    sar: str = "1/1",
    webm: bool = False,
) -> None:
    arguments = [
        "/usr/bin/ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-filter_threads",
        "1",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={size}:rate=24:duration=0.7",
    ]
    if audio:
        arguments += ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.7"]
    arguments += [
        "-vf",
        f"setsar={sar}",
        "-c:v",
        "libvpx-vp9" if webm else "libx264",
        "-threads:v",
        "1",
        "-pix_fmt",
        "yuv420p",
    ]
    if audio:
        arguments += ["-c:a", "libopus" if webm else "aac", "-threads:a", "1"]
    arguments += ["-y", str(target)]
    subprocess.run(arguments, check=True, capture_output=True, timeout=20)


@pytest.mark.parametrize(
    ("size", "audio", "sar", "webm", "expected"),
    [
        ("320x180", True, "1/1", False, (320, 180)),
        ("180x320", False, "1/1", False, (180, 320)),
        ("320x240", False, "4/3", False, (320, 180)),
        ("320x180", True, "1/1", True, (320, 180)),
        ("1440x810", False, "1/1", False, (1280, 720)),
        ("810x1440", False, "1/1", False, (720, 1280)),
    ],
)
def test_real_transcode(
    tmp_path: Path,
    size: str,
    audio: bool,
    sar: str,
    webm: bool,
    expected: tuple[int, int],
) -> None:
    source = tmp_path / ("source.webm" if webm else "source.mp4")
    _make_video(source, size=size, audio=audio, sar=sar, webm=webm)
    work = tmp_path / "attempt"
    calls = 0

    def checkpoint() -> None:
        nonlocal calls
        calls += 1

    info = transcode(source, work, checkpoint)
    assert (info.width, info.height) == expected
    assert info.has_audio is audio
    assert 0.5 <= info.duration <= 1.0
    assert calls > 4
    assert (work / "poster.jpg").stat().st_size > 100
    # A decoder traverses the result, not just its metadata.
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(work / "playback.mp4"),
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )


def test_real_rotated_mov(tmp_path: Path) -> None:
    source, rotated = tmp_path / "source.mp4", tmp_path / "rotated.mov"
    _make_video(source, audio=False)
    subprocess.run(
        [
            "/usr/bin/ffmpeg",
            "-v",
            "error",
            "-i",
            str(source),
            "-c",
            "copy",
            "-metadata:s:v:0",
            "rotate=90",
            str(rotated),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    info = transcode(rotated, tmp_path / "attempt", lambda: None)
    assert (info.width, info.height) == (180, 320)
    data = json.loads(
        subprocess.check_output(
            [
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(tmp_path / "attempt/playback.mp4"),
            ],
            timeout=10,
        )
    )
    assert not any(
        item.get("rotation", 0) for item in data["streams"][0].get("side_data_list", [])
    )


def test_webm_without_duration_metadata(tmp_path: Path) -> None:
    source = tmp_path / "recording.webm"
    # An unseekable WebM has no duration header, like a MediaRecorder upload.
    source.write_bytes(
        subprocess.check_output(
            [
                "/usr/bin/ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=180x320:rate=24:duration=0.7",
                "-c:v",
                "libvpx-vp9",
                "-threads",
                "1",
                "-f",
                "webm",
                "pipe:1",
            ],
            timeout=20,
        )
    )
    info = transcode(source, tmp_path / "attempt", lambda: None)
    assert (info.width, info.height) == (180, 320)
    assert 0.5 <= info.duration <= 1.0


@pytest.mark.parametrize(
    "contents",
    [
        b"not a video",
        b"#EXTM3U\n#EXTINF:3,\nhttp://127.0.0.1:9/private.mp4\n",
        b"ffconcat version 1.0\nfile '/etc/passwd'\n",
    ],
)
def test_non_video_and_external_playlists_rejected(
    tmp_path: Path, contents: bytes
) -> None:
    source = tmp_path / "looks-like.mp4"
    source.write_bytes(contents)
    with pytest.raises(InvalidVideo):
        transcode(source, tmp_path / "attempt", lambda: None)
    assert not (tmp_path / "attempt/playback.mp4").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration", "1201"),
        ("duration", "nan"),
        ("duration", "inf"),
        ("width", 1922),
        ("height", 1922),
        ("avg_frame_rate", "61/1"),
        ("sample_aspect_ratio", "1:0"),
        ("codec_name", "png"),
    ],
)
def test_limits_checked_before_encoding(field: str, value: object) -> None:
    video = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "h264",
        "width": 320,
        "height": 180,
        "avg_frame_rate": "24/1",
        "r_frame_rate": "24/1",
        "sample_aspect_ratio": "1:1",
    }
    container = {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1.0"}
    if field == "duration":
        container[field] = value  # type: ignore[assignment]
    else:
        video[field] = value
    with pytest.raises(InvalidVideo):
        _inspect({"format": container, "streams": [video]})


def test_capture_is_bounded_and_environment_has_no_secrets() -> None:
    output = _run(
        [sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"],
        5,
        lambda: None,
        capture=True,
    )
    environment = json.loads(output)
    assert set(environment) <= {"PATH", "LANG", "LC_ALL", "LC_CTYPE"}
    with pytest.raises(InvalidVideo):
        _run(
            [sys.executable, "-c", "import os; os.write(1,b'x'*1000000)"],
            5,
            lambda: None,
            capture=True,
        )
    with pytest.raises(InvalidVideo):
        _run(
            [sys.executable, "-c", "import os; os.write(2,b'x'*2000000)"],
            5,
            lambda: None,
        )


def test_checkpoint_interrupt_reaps_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    program = (
        "import os,time,pathlib; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(20)"
    )

    def checkpoint() -> None:
        if pid_file.exists():
            raise TranscodeInterrupted()

    started = time.monotonic()
    with pytest.raises(TranscodeInterrupted):
        _run([sys.executable, "-c", program], 30, checkpoint)
    assert time.monotonic() - started < 3
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, signal.SIGCONT)


def test_subprocess_timeout_is_bounded() -> None:
    started = time.monotonic()
    with pytest.raises(InvalidVideo, match="время"):
        _run([sys.executable, "-c", "import time;time.sleep(20)"], 0.2, lambda: None)
    assert time.monotonic() - started < 3


def test_attempt_does_not_overwrite_existing_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    _make_video(source)
    work = tmp_path / "attempt"
    work.mkdir()
    (work / "playback.mp4").write_bytes(b"existing attempt")
    with pytest.raises(InvalidVideo):
        transcode(source, work, lambda: None)
    assert (work / "playback.mp4").read_bytes() == b"existing attempt"
