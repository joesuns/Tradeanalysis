"""Load Excel column header comments from YAML glossary."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_YAML = (
    Path(__file__).resolve().parent.parent / "docs" / "export" / "export-column-comments.yaml"
)

# openpyxl Comment box size (pixels). Excel default ~108×79 shows only a few lines.
DEFAULT_COMMENT_WIDTH = 480
DEFAULT_COMMENT_HEIGHT = 300


@dataclass(frozen=True)
class _CommentBundle:
    descriptions: Dict[str, str]
    metadata: dict


def _parse_columns(columns: object) -> Dict[str, str]:
    if not isinstance(columns, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in columns.items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[str(key)] = text
    return out


@lru_cache(maxsize=1)
def _load_bundle(yaml_path: Optional[str] = None) -> _CommentBundle:
    path = Path(yaml_path) if yaml_path else _DEFAULT_YAML
    if not path.is_file():
        logger.warning(
            "export column comments YAML missing: %s — exporting without header comments",
            path,
        )
        return _CommentBundle(descriptions={}, metadata={})
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"invalid export column comments YAML: {path}")
    common = data.get("_common") or {}
    if not isinstance(common, dict):
        common = {}
    return _CommentBundle(
        descriptions=_parse_columns(data.get("columns")),
        metadata=common,
    )


def clear_comment_cache() -> None:
    """Clear cached YAML bundle (tests only)."""
    _load_bundle.cache_clear()


def get_comment_metadata(yaml_path: Optional[str] = None) -> dict:
    """Return _common block (author, footer, weekly_prefix)."""
    return dict(_load_bundle(yaml_path).metadata)


def get_column_descriptions(yaml_path: Optional[str] = None) -> Dict[str, str]:
    """Return {english_col_key: description_text} from YAML."""
    return dict(_load_bundle(yaml_path).descriptions)


def format_column_comment(
    col: str,
    *,
    weekly: bool = False,
    yaml_path: Optional[str] = None,
) -> Optional[str]:
    """Build full comment text for one export column header."""
    bundle = _load_bundle(yaml_path)
    body = bundle.descriptions.get(col)
    if not body:
        return None
    meta = bundle.metadata
    if weekly:
        prefix = str(meta.get("weekly_prefix") or "【周线】").strip()
        if prefix:
            body = f"{prefix}{body}"
    footer = str(meta.get("footer") or "").strip()
    if footer:
        return f"{body}\n\n{footer}"
    return body


def comment_author(yaml_path: Optional[str] = None) -> str:
    meta = _load_bundle(yaml_path).metadata
    return str(meta.get("author") or "Tradeanalysis")


def comment_box_size(yaml_path: Optional[str] = None) -> Tuple[int, int]:
    """Return (width, height) in pixels for Excel header comment popups."""
    meta = _load_bundle(yaml_path).metadata
    try:
        width = int(meta.get("comment_width") or DEFAULT_COMMENT_WIDTH)
    except (TypeError, ValueError):
        width = DEFAULT_COMMENT_WIDTH
    try:
        height = int(meta.get("comment_height") or DEFAULT_COMMENT_HEIGHT)
    except (TypeError, ValueError):
        height = DEFAULT_COMMENT_HEIGHT
    return max(width, 108), max(height, 79)
