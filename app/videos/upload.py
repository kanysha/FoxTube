import os
import time
from pathlib import Path
from typing import BinaryIO

from fastapi import HTTPException
from sqlalchemy import Engine

from app.videos import service
from app.videos.storage import media_path, sync_directory


class UploadSink:
    """One bounded stream on disk. Its methods run in the thread pool."""

    def __init__(self, engine: Engine, root: Path, ticket: service.UploadTicket):
        self.engine = engine
        self.root = root
        self.ticket = ticket
        self.started = time.monotonic()
        self.next_heartbeat = self.started + 10
        self.count = 0
        self.stream: BinaryIO | None = None

    def open(self) -> None:
        path = media_path(self.root, self.ticket.part_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        self.stream = os.fdopen(descriptor, "wb")

    def write(self, chunk: bytes) -> None:
        now = time.monotonic()
        if now - self.started > 1800:
            raise HTTPException(408, "Передача превысила 30 минут. Повторите загрузку.")
        if self.count + len(chunk) > self.ticket.expected_size:
            raise HTTPException(413, "Файл больше заявленного размера.")
        if now >= self.next_heartbeat:
            service.renew_upload(self.engine, self.ticket)
            self.next_heartbeat = now + 10
        assert self.stream is not None
        self.stream.write(chunk)
        self.count += len(chunk)

    def publish(self) -> None:
        if self.count != self.ticket.expected_size:
            raise HTTPException(
                400, "Передача прервана: размер файла не совпадает с заявленным."
            )
        assert self.stream is not None
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        self.stream = None
        source = media_path(self.root, self.ticket.source_key)
        source.parent.mkdir(parents=True, exist_ok=True)
        os.replace(media_path(self.root, self.ticket.part_key), source)
        sync_directory(source.parent)
        service.finish_upload(self.engine, self.ticket)

    def close(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        media_path(self.root, self.ticket.part_key).unlink(missing_ok=True)
        # The source may have committed even if a lost connection hid COMMIT's
        # acknowledgement. Never remove it here; the worker reconciles orphans.
        if service.abort_upload(self.engine, self.ticket):
            media_path(self.root, self.ticket.source_key).unlink(missing_ok=True)
