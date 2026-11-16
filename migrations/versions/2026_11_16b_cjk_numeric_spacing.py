"""merge canonical questions after CJK/numeric spacing normalization

Revision ID: 2026_11_16b
Revises: 2026_11_16
Create Date: 2026-11-16 01:00:00.000000
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "source_fingerprints_migration",
    Path(__file__).with_name("2026_11_16_source_fingerprints.py"),
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
_merge_format_only_duplicates = _module._merge_format_only_duplicates

revision = "2026_11_16b"
down_revision = "2026_11_16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _merge_format_only_duplicates()


def downgrade() -> None:
    pass
