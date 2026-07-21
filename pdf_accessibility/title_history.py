from __future__ import annotations

import json
from pathlib import Path

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
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text('{"titles": []}\n', encoding="utf-8")
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
    titles = _read_title_history(path)
    return titles if limit is None else titles[-max(1, limit) :]


def remember_title(
    path: Path, title: str, limit: int | None = None
) -> list[str]:
    titles = _read_title_history(path)
    normalized = " ".join(title.split())
    titles = [item for item in titles if item != normalized]
    if normalized:
        titles.append(normalized)
    path.resolve().write_text(
        json.dumps({"titles": titles}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return titles if limit is None else titles[-max(1, limit) :]
