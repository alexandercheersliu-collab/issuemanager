"""公共种子题库生成与导入（SQL 权威 + 可重建向量）。

生成器为纯函数（确定性，random.Random(42)），可按学科×年级产出 ≥200 道
带完整题干、答案、解析和 K12 元数据的模板变体题。导入按规范化题干哈希幂等复用
SQL 标准题，再以 canonical_questions.id 写入 ChromaDB；不完整条目不会入库。

用法：
    python scripts/seed_question_bank.py            # 生成并导入公共题库
    python scripts/seed_question_bank.py --dry-run  # 只打印统计，不写入
"""
from __future__ import annotations

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------- 模板题生成（纯函数，参数显式传入便于单测答案正确性） ----------


def add_item(a: int, b: int) -> tuple[str, str]:
    return f"计算 {a} + {b} 的结果。", str(a + b)


def sub_item(a: int, b: int) -> tuple[str, str]:
    a, b = max(a, b), min(a, b)
    return f"计算 {a} - {b} 的结果。", str(a - b)


def mul_item(a: int, b: int) -> tuple[str, str]:
    return f"计算 {a} × {b} 的结果。", str(a * b)


def div_item(a: int, b: int) -> tuple[str, str]:
    return f"计算 {a * b} ÷ {b} 的结果。", str(a)


def percent_item(a: int, b: int) -> tuple[str, str]:
    value = a * b / 100
    text = f"{a} 的 {b}% 是多少？"
    return text, str(int(value) if value == int(value) else value)


def linear_eq_item(a: int, x: int, b: int) -> tuple[str, str]:
    return f"解方程：{a}x + {b} = {a * x + b}。", f"x = {x}"


def sqrt_item(a: int) -> tuple[str, str]:
    return f"求 {a * a} 的平方根。", f"±{a}"


def quadratic_item(x1: int, x2: int) -> tuple[str, str]:
    s, p = x1 + x2, x1 * x2
    sign_s = "-" if s >= 0 else "+"
    sign_p = "+" if p >= 0 else "-"
    return (
        f"解方程：x² {sign_s} {abs(s)}x {sign_p} {abs(p)} = 0。",
        f"x₁ = {x1}，x₂ = {x2}",
    )


def linear_func_item(a: int, b: int, c: int) -> tuple[str, str]:
    return f"已知 f(x) = {a}x + {b}，求 f({c})。", str(a * c + b)


def derivative_item(a: int, b: int) -> tuple[str, str]:
    return f"求 f(x) = x² + {a}x 在 x = {b} 处的导数。", str(2 * b + a)


def arithmetic_seq_item(a1: int, d: int, n: int) -> tuple[str, str]:
    return f"等差数列首项为 {a1}，公差为 {d}，求第 {n} 项。", str(a1 + (n - 1) * d)


def speed_item(v: int, t: int) -> tuple[str, str]:
    return f"物体在 {t} s 内匀速通过 {v * t} m，求平均速度（m/s）。", str(v)


def newton_item(m: int, a: int) -> tuple[str, str]:
    return (
        f"质量为 {m} kg 的物体受到 {m * a} N 的合力，求加速度（m/s²）。",
        str(a),
    )


def mole_mass_item(n: int) -> tuple[str, str]:
    return f"{n} mol 水的质量是多少克？（H₂O 的摩尔质量为 18 g/mol）", f"{18 * n} g"


def concentration_item(k: int, v: int) -> tuple[str, str]:
    return (
        f"将 {58.5 * k:g} g NaCl 溶于水配成 {v} L 溶液，求物质的量浓度"
        "（NaCl 的摩尔质量取 58.5 g/mol）。",
        f"{k / v:g} mol/L",
    )


def shopping_item(price: int, n: int) -> tuple[str, str]:
    return (
        f"超市里每盒牛奶 {price} 元，妈妈买了 {n} 盒，一共需要付多少元？",
        f"{price * n} 元",
    )


def trip_item(v: int, t: int) -> tuple[str, str]:
    return (
        f"一辆汽车以每小时 {v} 千米的速度行驶了 {t} 小时，一共行驶了多少千米？",
        f"{v * t} 千米",
    )


# ---------- 年级×学科覆盖表 ----------

# (学科, 年级范围, 知识点, 难度池, 题型, 参数采样器 → (题干, 答案))
_TEMPLATES = [
    ("math", range(1, 3), ["加法", "100以内加减法"], ["easy"], "计算题",
     lambda r: add_item(r.randint(2, 60), r.randint(2, 39))),
    ("math", range(1, 3), ["减法", "100以内加减法"], ["easy"], "计算题",
     lambda r: sub_item(r.randint(2, 60), r.randint(2, 60))),
    ("math", range(3, 5), ["乘法", "多位数乘法"], ["easy", "medium"], "计算题",
     lambda r: mul_item(r.randint(3, 25), r.randint(3, 19))),
    ("math", range(3, 5), ["除法", "除数是一位数"], ["easy", "medium"], "计算题",
     lambda r: div_item(r.randint(3, 20), r.randint(2, 12))),
    ("math", range(3, 7), ["单价×数量=总价", "小数乘法", "应用题"], ["easy", "medium"], "应用题",
     lambda r: shopping_item(r.randint(2, 15), r.randint(2, 9))),
    ("math", range(4, 7), ["路程=速度×时间", "行程问题", "应用题"], ["medium"], "应用题",
     lambda r: trip_item(r.choice([40, 45, 50, 55, 60, 65, 70, 80]), r.randint(2, 6))),
    ("math", range(5, 7), ["百分数", "百分数应用"], ["medium"], "计算题",
     lambda r: percent_item(r.choice([40, 60, 80, 120, 150, 200]), r.choice([5, 10, 15, 20, 25, 30]))),
    ("math", range(5, 7), ["分数运算"], ["medium"], "计算题",
     lambda r: mul_item(r.randint(2, 9), r.randint(2, 9))),
    ("math", range(7, 8), ["一元一次方程"], ["medium"], "计算题",
     lambda r: linear_eq_item(r.randint(2, 9), r.randint(-6, 8), r.randint(-9, 9))),
    ("math", range(8, 9), ["平方根", "实数"], ["easy", "medium"], "计算题",
     lambda r: sqrt_item(r.randint(2, 20))),
    ("math", range(9, 10), ["一元二次方程", "求根公式"], ["medium", "hard"], "计算题",
     lambda r: quadratic_item(r.randint(-5, 6), r.randint(-5, 6))),
    ("math", range(10, 11), ["一次函数", "函数值"], ["medium"], "计算题",
     lambda r: linear_func_item(r.randint(2, 7), r.randint(-8, 8), r.randint(-5, 6))),
    ("math", range(11, 12), ["导数", "导数运算"], ["medium", "hard"], "计算题",
     lambda r: derivative_item(r.randint(-6, 7), r.randint(-4, 5))),
    ("math", range(12, 13), ["等差数列", "数列通项"], ["medium", "hard"], "计算题",
     lambda r: arithmetic_seq_item(r.randint(1, 9), r.randint(2, 7), r.randint(5, 15))),
    ("physics", range(7, 10), ["速度", "匀速直线运动"], ["easy", "medium"], "应用题",
     lambda r: speed_item(r.randint(2, 20), r.randint(2, 12))),
    ("physics", range(10, 13), ["牛顿第二定律"], ["medium", "hard"], "应用题",
     lambda r: newton_item(r.randint(1, 10), r.randint(1, 9))),
    ("chemistry", range(9, 10), ["物质的量", "摩尔质量"], ["medium"], "计算题",
     lambda r: mole_mass_item(r.randint(1, 8))),
    ("chemistry", range(10, 13), ["物质的量浓度"], ["medium", "hard"], "计算题",
     lambda r: concentration_item(r.randint(1, 4), r.choice([1, 2]))),
]

# 每个（学科, 年级）组合生成的变体数
_VARIANTS_PER_CELL = {"math": 12, "physics": 6, "chemistry": 6}


def generate_bank_items(seed: int = 42) -> list[dict]:
    """按年级×学科生成确定性种子题（≥200 道，字段含完整 K12 元数据）。"""
    rng = random.Random(seed)
    items: list[dict] = []
    counters: dict[tuple[str, int], int] = {}
    for subject, grades, points, difficulties, question_type, sampler in _TEMPLATES:
        for grade in grades:
            for _ in range(_VARIANTS_PER_CELL[subject]):
                content, answer = sampler(rng)
                key = (subject, grade)
                counters[key] = counters.get(key, 0) + 1
                items.append(
                    {
                        "bank_id": f"{subject}-g{grade}-{counters[key]:03d}",
                        "content": content,
                        "analysis": f"根据{points[0]}的运算法则逐步计算，得到答案：{answer}。",
                        "answer": answer,
                        "subject": subject,
                        "grade": grade,
                        "knowledge_points": list(points),
                        "difficulty": rng.choice(difficulties),
                        "question_type": question_type,
                        "chapter": points[0],
                    }
                )
    return items


def import_bank_items(items: list[dict], *, session_factory, vector_store) -> dict:
    """把完整种子写入权威 SQL，并以标准题 SQL ID 重建向量。"""
    from backend.repositories.question_bank import QuestionBankRepository

    stats = {"created": 0, "reused": 0, "skipped_incomplete": 0, "indexed": 0}
    canonicals = []
    with session_factory() as session:
        repository = QuestionBankRepository(session)
        for item in items:
            if not all(str(item.get(key) or "").strip() for key in ("content", "analysis", "answer")):
                stats["skipped_incomplete"] += 1
                continue
            canonical = repository.find_by_stem(item["content"])
            if canonical is None:
                canonical = repository.create(
                    stem_markdown=item["content"],
                    analysis_markdown=item["analysis"],
                    answer=item["answer"],
                    knowledge_points=list(item.get("knowledge_points") or []),
                    difficulty=item.get("difficulty") or "medium",
                    question_type=item.get("question_type"),
                    chapter=item.get("chapter"),
                    subject=item.get("subject") or "math",
                    grade=item.get("grade"),
                    origin="seed",
                    validation_state="machine_validated",
                )
                stats["created"] += 1
            else:
                was_incomplete = not repository.is_reusable(canonical)
                for field, value in (
                    ("analysis_markdown", item["analysis"]),
                    ("answer", item["answer"]),
                    ("knowledge_points", list(item.get("knowledge_points") or [])),
                    ("question_type", item.get("question_type")),
                    ("chapter", item.get("chapter")),
                    ("grade", item.get("grade")),
                ):
                    current = getattr(canonical, field)
                    empty = not current or (isinstance(current, str) and not current.strip())
                    if empty and value:
                        setattr(canonical, field, value)
                if was_incomplete and canonical.origin == "student":
                    canonical.origin = "seed"
                stats["reused"] += 1
            canonicals.append(canonical)
        session.commit()
        canonicals = [
            {
                "id": canonical.id,
                "stem": canonical.stem_markdown,
                "subject": canonical.subject,
                "grade": canonical.grade,
                "region": canonical.region,
                "knowledge_points": list(canonical.knowledge_points or []),
                "difficulty": canonical.difficulty,
                "answer": canonical.answer,
                "question_type": canonical.question_type or "",
                "chapter": canonical.chapter or "",
            }
            for canonical in canonicals
        ]

    for canonical in canonicals:
        indexed = vector_store.upsert_bank_question(
            str(canonical["id"]),
            canonical["stem"],
            subject=canonical["subject"],
            grade=canonical["grade"],
            region=canonical["region"],
            knowledge_points=canonical["knowledge_points"],
            difficulty=canonical["difficulty"],
            answer=canonical["answer"],
            question_type=canonical["question_type"],
            chapter=canonical["chapter"],
        )
        stats["indexed"] += int(indexed)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成并导入公共种子题库（SQL + ChromaDB）")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写入 SQL 或向量库")
    args = parser.parse_args(argv)

    items = generate_bank_items()
    by_subject: dict[str, int] = {}
    by_grade: dict[int, int] = {}
    for item in items:
        by_subject[item["subject"]] = by_subject.get(item["subject"], 0) + 1
        by_grade[item["grade"]] = by_grade.get(item["grade"], 0) + 1
    print(f"共生成 {len(items)} 道种子题")
    print(f"按学科：{dict(sorted(by_subject.items()))}")
    print(f"按年级：{dict(sorted(by_grade.items()))}")

    if args.dry_run:
        print("dry-run：未写入 SQL 或向量库")
        return 0

    from backend.database import SessionLocal, init_db
    from backend.services.rag import QuestionVectorStore

    init_db()
    store = QuestionVectorStore()
    stats = import_bank_items(items, session_factory=SessionLocal, vector_store=store)
    expected = len(items) - stats["skipped_incomplete"]
    print(
        f"SQL 新建 {stats['created']}，复用 {stats['reused']}，"
        f"跳过不完整 {stats['skipped_incomplete']}；"
        f"向量成功 {stats['indexed']}/{expected}，当前总量：{store.bank_size()}"
    )
    return 0 if stats["indexed"] == expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
