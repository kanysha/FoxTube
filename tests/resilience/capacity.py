"""Capacity smoke test with measured host resources, not a general SLA claim."""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from run import Client, Run

SOURCE_BYTES = 500 * 1024 * 1024


class Capacity(Run):
    def rss(self) -> int:
        program = """
import os
from pathlib import Path
total = 0
for process in Path('/proc').iterdir():
    try:
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        command = (process / 'cmdline').read_bytes().split(b'\\0')
        if not command or b'python' not in command[0]:
            continue
        for line in (process / 'status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                total += int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
print(total)
"""
        return int(self.compose("exec", "-T", "api", "python", "-c", program))

    def draft(self, owner: Client, size: int, title: str) -> str:
        return str(
            owner.json(
                "POST",
                "/api/v1/videos",
                expected=201,
                data={
                    "title": title,
                    "visibility": "public",
                    "expected_size": size,
                },
            )["id"]
        )

    def transfer(self, owner: Client, video_id: str, size: int, rate: int) -> None:
        # A final ISO BMFF free box pads the real MP4 without allocating 500 MiB
        # in the client or changing the encoded frames.
        def chunks() -> Iterator[bytes]:
            started = time.monotonic()
            sent = 0
            with self.args.source.open("rb") as stream:
                while block := stream.read(64 * 1024):
                    yield block
                    sent += len(block)
                    time.sleep(max(0, sent / rate - (time.monotonic() - started)))
            padding = size - sent
            if padding:
                assert padding >= 8
                yield struct.pack(">I4s", padding, b"free")
                sent += 8
                while sent < size:
                    block = bytes(min(64 * 1024, size - sent))
                    yield block
                    sent += len(block)
                    time.sleep(max(0, sent / rate - (time.monotonic() - started)))

        owner.request(
            "PUT",
            f"/api/v1/videos/{video_id}/source",
            data=chunks(),
            expected=202,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            },
        )

    def run(self) -> None:
        host = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "info",
                    "--format",
                    '{"cpus":{{.NCPU}},"memory_bytes":{{.MemTotal}}}',
                ],
                timeout=20,
            )
        )
        self.report["host_resources"] = host
        self.report["limitations"] = [
            "Measured resources describe the Docker host visible to this run; "
            "a shared host or VM is not a dedicated production capacity guarantee.",
            "The stdlib load generator runs on the machine invoking this "
            "command; an external Internet path is not measured.",
            "Ten HTTP viewers fetch 64 KiB ranges every 200 ms, not ten "
            "interactive browsers or an Internet uplink benchmark.",
            "The 500 MiB input contains a real 45-second H.264 stream plus "
            "a valid MP4 free box; this measures upload memory, not "
            "worst-case decoding cost.",
        ]
        self.fixture()
        port = self.compose("port", "nginx", "8080").decode().strip().rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        owners = [Client(base) for _ in range(4)]
        for index, owner in enumerate(owners):
            owner.register(f"capacity-{index}@example.com")
        source_size = self.args.source.stat().st_size
        reference = self.draft(owners[0], source_size, "Reference for ten viewers")
        self.transfer(owners[0], reference, source_size, 64 * 1024 * 1024)
        self.ready(reference)
        # Warm both API processes before the RSS baseline.
        guest = Client(base)
        for _ in range(12):
            guest.request("HEAD", f"/media/videos/{reference}/playback")
        baseline = self.rss()
        assert baseline > 0, "API RSS sampler found no Python processes"

        background = self.draft(owners[1], source_size, "Concurrent encoder")
        large = self.draft(owners[2], SOURCE_BYTES, "500 MiB streamed upload")
        other = self.draft(owners[3], source_size, "Second simultaneous upload")
        started = time.monotonic()
        stop = threading.Event()
        viewer_barrier = threading.Barrier(11)
        resource_samples: list[dict[str, Any]] = []
        api_latencies: list[float] = []
        monitor_errors: list[str] = []
        container_ids = self.compose("ps", "-q", "api", "worker", "nginx").split()
        assert len(container_ids) == 3

        def container_states() -> list[dict[str, Any]]:
            rows = json.loads(
                subprocess.check_output(
                    ["docker", "inspect", *(value.decode() for value in container_ids)],
                    timeout=20,
                )
            )
            return [
                {
                    "name": row["Name"],
                    "restarts": row["RestartCount"],
                    "oom_killed": row["State"]["OOMKilled"],
                    "running": row["State"]["Running"],
                }
                for row in rows
            ]

        initial_states = container_states()

        def monitor() -> None:
            client = Client(base)
            try:
                while not stop.is_set():
                    request_started = time.monotonic()
                    client.json("GET", "/api/v1/videos?page_size=1")
                    api_latencies.append(time.monotonic() - request_started)
                    output = subprocess.check_output(
                        [
                            "docker",
                            "stats",
                            "--no-stream",
                            "--format",
                            "{{json .}}",
                            *(value.decode() for value in container_ids),
                        ],
                        timeout=20,
                    )
                    for line in output.splitlines():
                        row = json.loads(line)
                        resource_samples.append(
                            {
                                "elapsed_seconds": round(time.monotonic() - started, 2),
                                "name": row["Name"],
                                "cpu_percent": row["CPUPerc"],
                                "memory_usage": row["MemUsage"],
                                "network_io": row["NetIO"],
                            }
                        )
                    stop.wait(2)
            except Exception as error:
                monitor_errors.append(type(error).__name__)

        def view() -> dict[str, Any]:
            client = Client(base)
            path = f"/media/videos/{reference}/playback"
            _, headers = client.request("HEAD", path)
            length = int(headers["Content-Length"])
            viewer_barrier.wait(timeout=30)
            count, received, maximum_latency = 0, 0, 0.0
            offset = 0
            while not stop.is_set():
                request_started = time.monotonic()
                end = min(offset + 65535, length - 1)
                data, response_headers = client.request(
                    "GET",
                    path,
                    headers={"Range": f"bytes={offset}-{end}"},
                    expected=206,
                )
                assert len(data) == end - offset + 1
                assert response_headers["Content-Range"].startswith(
                    f"bytes {offset}-{end}/"
                )
                maximum_latency = max(
                    maximum_latency, time.monotonic() - request_started
                )
                count += 1
                received += len(data)
                offset = (end + 1) % length
                stop.wait(0.2)
            return {
                "requests": count,
                "bytes": received,
                "max_latency_seconds": round(maximum_latency, 3),
            }

        samples = [baseline]
        overlap = False
        monitoring = threading.Thread(target=monitor, daemon=True)
        monitoring.start()
        with ThreadPoolExecutor(max_workers=12) as pool:
            viewers = [pool.submit(view) for _ in range(10)]
            try:
                viewer_barrier.wait(timeout=30)
                self.transfer(owners[1], background, source_size, 64 * 1024 * 1024)
                self.wait(
                    lambda: self.active_ffmpeg(background),
                    "active encoder before concurrent uploads",
                    timeout=30,
                )
                uploads = [
                    pool.submit(
                        self.transfer, owners[2], large, SOURCE_BYTES, 16 * 1024 * 1024
                    ),
                    pool.submit(
                        self.transfer, owners[3], other, source_size, 512 * 1024
                    ),
                ]
                while not all(upload.done() for upload in uploads):
                    samples.append(self.rss())
                    if not overlap:
                        active = int(
                            self.sql(
                                "SELECT count(*) FROM videos WHERE "
                                "upload_token IS NOT NULL AND deleted_at IS NULL;"
                            )
                        )
                        overlap = active == 2 and self.active_ffmpeg(background)
                    time.sleep(0.5)
                for upload in uploads:
                    upload.result()
            finally:
                stop.set()
                monitoring.join(timeout=25)
            viewer_results = [viewer.result() for viewer in viewers]
        peak = max(samples)
        growth = peak - baseline
        assert overlap, "Did not observe two active uploads during real encoding"
        assert all(item["requests"] >= 10 for item in viewer_results)
        assert len(samples) >= 10
        assert growth < 128 * 1024 * 1024, (
            "API RSS grew excessively during 500 MiB upload"
        )
        assert peak < 512 * 1024 * 1024, "API Python RSS exceeded 512 MiB"
        assert not monitoring.is_alive(), "Resource monitor did not stop"
        assert not monitor_errors, f"Resource monitor failed: {monitor_errors}"
        assert len(resource_samples) >= 6 and len(api_latencies) >= 2
        for video_id in (background, large, other):
            self.ready(video_id)
        final_states = container_states()
        assert initial_states == final_states, "Application containers restarted"
        assert all(row["running"] and not row["oom_killed"] for row in final_states)
        self.passed(
            "500 MiB streaming, two uploads, real encoder and ten HTTP viewers",
            started,
            input_bytes=SOURCE_BYTES,
            rss_baseline_bytes=baseline,
            rss_peak_bytes=peak,
            rss_growth_bytes=growth,
            rss_samples=len(samples),
            concurrent_uploads_observed=2,
            active_encoder_observed=True,
            viewers=viewer_results,
            all_uploads_ready=True,
            api_requests=len(api_latencies),
            api_max_latency_seconds=round(max(api_latencies), 3),
            resource_samples=resource_samples,
            container_states=final_states,
        )
        self.report["status"] = "passed"
        self.write_report()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    run = Capacity(parser.parse_args())
    try:
        run.run()
    except Exception as error:
        run.report["status"] = "failed"
        run.report["failure"] = f"{type(error).__name__}: {error}"
        run.write_report()
        print(f"Failed: {type(error).__name__}: {error}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
