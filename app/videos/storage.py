import os
from pathlib import Path


def media_path(root: Path, key: str) -> Path:
    """Keys come exclusively from the server; reject traversal and symlinks anyway."""
    base = root.resolve()
    path = base / key
    if not key or Path(key).is_absolute() or ".." in Path(key).parts:
        raise ValueError("Invalid media key")
    if not path.resolve().is_relative_to(base):
        raise ValueError("Media path outside storage")
    current = path
    while current != base:
        if current.is_symlink():
            raise ValueError("Symlink in media path")
        current = current.parent
    return path


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
