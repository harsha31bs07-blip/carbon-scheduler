"""Bounded content identity for cached evidence snapshots."""

import hashlib
from pathlib import Path


def evidence_stamp(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Evidence directory is unavailable")
    digest, total, count = hashlib.sha256(), 0, 0
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        # Live SQLite queues and the runner lock are not portable report inputs.
        if relative.parts[0] in {"queues", "quality-review-history"} or path.name.startswith(".runner"):
            continue
        if path.is_symlink():
            raise ValueError("Evidence cannot follow symlinks")
        if not path.is_file():
            continue
        count += 1
        size = path.stat().st_size
        total += size
        if count > 4096 or total > 64 * 1024 * 1024:
            raise ValueError("Evidence cache exceeds the local inspection bound")
        digest.update(str(relative).replace("\\", "/").encode())
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def retained_evidence(state):
    retained = state.get("validated_comparison")
    if retained is None:
        return None
    try:
        if evidence_stamp(retained[0]) != state.get("validated_comparison_stamp"):
            raise ValueError("Evidence changed after validation")
    except (ValueError, OSError):
        state.pop("validated_comparison", None)
        state.pop("validated_comparison_stamp", None)
        raise ValueError("Evidence changed or became unavailable; load and validate it again") from None
    return retained
