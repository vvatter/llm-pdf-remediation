from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Iterator


@contextmanager
def _history_lock(path: Path) -> Iterator[Path]:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock-v2")
    deadline = time.monotonic() + 30
    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.close(descriptor)
            break
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > 300
            except FileNotFoundError:
                continue
            if stale:
                lock_path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for recent-title lock: {lock_path}")
            time.sleep(0.02)
    try:
        yield path
    finally:
        lock_path.unlink(missing_ok=True)


def _write_title_history(path: Path, titles: list[str]) -> None:
    payload = json.dumps({"titles": titles}, ensure_ascii=False, indent=2) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)

def _clean_titles(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        title = " ".join(str(value).split())
        if title and title not in result:
            result.append(title)
    return result


def _read_title_history(path: Path) -> list[str]:
    if not path.exists():
        _write_title_history(path, [])
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"recent titles file is not valid JSON: {path}") from error
    return _clean_titles(data.get("titles") if isinstance(data, dict) else None)


def load_recent_titles(
    path: Path, limit: int | None = None
) -> list[str]:
    """Create and return the local title history used as prompt context."""
    with _history_lock(path) as resolved_path:
        titles = _read_title_history(resolved_path)
    return titles if limit is None else titles[-max(1, limit) :]


def remember_title(
    path: Path, title: str, limit: int | None = None
) -> list[str]:
    with _history_lock(path) as resolved_path:
        titles = _read_title_history(resolved_path)
        normalized = " ".join(title.split())
        titles = [item for item in titles if item != normalized]
        if normalized:
            titles.append(normalized)
        _write_title_history(resolved_path, titles)
    return titles if limit is None else titles[-max(1, limit) :]
