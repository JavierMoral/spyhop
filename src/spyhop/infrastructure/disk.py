"""Free disk space where a directory lives or would be created."""

import shutil
from pathlib import Path

GIB = 1024**3


def free_bytes(path: Path) -> int:
    """Free space on the filesystem of ``path``, or of its nearest existing
    ancestor when it does not exist yet."""

    for candidate in (path, *path.parents):
        if candidate.exists():
            return shutil.disk_usage(candidate).free

    return shutil.disk_usage(Path.home()).free
