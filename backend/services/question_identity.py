"""共享题库的题干规范化与稳定身份。"""
from __future__ import annotations

import hashlib
import re
import unicodedata

from backend.services.ai.base import normalize_math_delimiters


def normalize_stem(text: str) -> str:
    """生成仅用于精确匹配的题干规范形式。"""
    normalized = unicodedata.normalize("NFC", text)
    normalized = normalize_math_delimiters(normalized)
    normalized = re.sub(r"^\s*\d+\.(?!\d)\s*", "", normalized, count=1)
    normalized = normalized.replace("（", "(").replace("）", ")")
    normalized = re.sub(r"\$(\d+(?:\.\d+)?)\$", r"\1", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.sub(r"(?<=\))\s+(?=[\u4e00-\u9fff])", "", normalized)
    normalized = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=\d)", "", normalized)
    normalized = re.sub(r"(?<=\d)\s+(?=[\u4e00-\u9fff])", "", normalized)
    if not normalized:
        raise ValueError("题干不能为空")
    return normalized


def stem_hash(text: str) -> str:
    """计算规范化题干的 UTF-8 SHA-256 摘要。"""
    return hashlib.sha256(normalize_stem(text).encode("utf-8")).hexdigest()
