"""公共种子题库生成器测试：数量/覆盖/字段/确定性/模板答案正确性。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from backend.models.schemas import SUBJECT_NAMES
from backend.services.rag import DIFFICULTY_ORDER

_SPEC = importlib.util.spec_from_file_location(
    "seed_question_bank",
    Path(__file__).resolve().parent.parent / "scripts" / "seed_question_bank.py",
)
seed = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(seed)


@pytest.fixture(scope="module")
def items():
    return seed.generate_bank_items()


def test_template_answers_correct():
    """模板生成函数的答案与题干自洽（抽查各类模板）。"""
    assert seed.add_item(2, 3) == ("计算 2 + 3 的结果。", "5")
    assert seed.mul_item(4, 5)[1] == "20"
    assert seed.div_item(6, 7) == ("计算 42 ÷ 7 的结果。", "6")
    assert seed.sub_item(3, 10)[1] == "7"  # 自动交换保证非负
    assert seed.linear_eq_item(2, 3, 1) == ("解方程：2x + 1 = 7。", "x = 3")
    assert seed.sqrt_item(4) == ("求 16 的平方根。", "±4")
    text, answer = seed.quadratic_item(1, 2)
    assert "x² - 3x + 2 = 0" in text and "x₁ = 1" in answer and "x₂ = 2" in answer
    assert seed.linear_func_item(2, 1, 3)[1] == "7"
    assert seed.derivative_item(3, 2)[1] == "7"  # f'(x)=2x+3, x=2
    assert seed.arithmetic_seq_item(1, 2, 5)[1] == "9"  # 1+(5-1)*2
    assert seed.speed_item(5, 4) == ("物体在 4 s 内匀速通过 20 m，求平均速度（m/s）。", "5")
    assert seed.newton_item(2, 3)[1] == "3"
    assert seed.mole_mass_item(2)[1] == "36 g"
    assert seed.concentration_item(2, 2)[1] == "1 mol/L"


def test_generate_at_least_200(items):
    assert len(items) >= 200


def test_bank_ids_unique(items):
    ids = [item["bank_id"] for item in items]
    assert len(ids) == len(set(ids))


def test_items_have_complete_k12_metadata(items):
    for item in items:
        assert item["content"].strip() and item["answer"].strip()
        assert item["subject"] in SUBJECT_NAMES
        assert 1 <= item["grade"] <= 12
        assert item["difficulty"] in DIFFICULTY_ORDER
        assert item["knowledge_points"]
        assert item["question_type"] and item["chapter"]


def test_grade_coverage_for_math(items):
    math_grades = {item["grade"] for item in items if item["subject"] == "math"}
    assert math_grades == set(range(1, 13))


def test_generation_is_deterministic():
    assert seed.generate_bank_items() == seed.generate_bank_items()


def test_main_dry_run(capsys):
    assert seed.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "共生成" in out and "dry-run" in out
