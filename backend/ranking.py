"""
ranking.py — '선택 SKU 취급 제조사' 페이지의 제조사 랭킹 계산 로직

랭킹은 현재 선택된 SKU와 유사한 SKU(similar_skus)를 취급하는 제조사 집단 안에서만
상대 비교한다 (MC 전체 비교 아님). 평가축은 다음 3가지이며 각 축은 A(3점)/B(2점)/C(1점)
등급으로 환산한 뒤 가중합으로 100점 만점 종합점수를 계산한다.

  ① 탑5 유통사 거래 다양성 (50%)
  ② 국내 수입횟수 — 유사 SKU 제조사 집단 내 상대 순위 (30%)
  ③ 최근 완료된 3개년 수입횟수 성장추세 (20%)
"""
from __future__ import annotations
from datetime import date
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TOP5_RETAILERS = ["이마트", "홈플러스", "롯데마트", "쿠팡", "코스트코"]

_GRADE_SCORE = {"A": 3, "B": 2, "C": 1}


def _top5_grade(distinct_top5_count: int) -> str:
    if distinct_top5_count >= 3:
        return "A"
    if distinct_top5_count >= 1:
        return "B"
    return "C"


def _growth_grade(y1: int, y2: int, y3: int) -> str:
    """y1, y2, y3는 시간순(가장 오래된 → 최근)."""
    if y1 < y2 < y3:
        return "A"
    if y1 > y2 > y3:
        return "C"
    return "B"


def _import_count_grades(counts_by_factory: dict[str, int]) -> dict[str, str]:
    """유사 SKU 제조사 집단 내 수입횟수 상대 순위로 A/B/C 등급 산출.
    동일 수입횟수는 동일 등급(PERCENT_RANK 방식: 동순위 처리)."""
    n = len(counts_by_factory)
    if n == 0:
        return {}

    ordered = sorted(counts_by_factory.items(), key=lambda kv: kv[1], reverse=True)

    grades: dict[str, str] = {}
    rank = 1
    prev_count = None
    for i, (factory, count) in enumerate(ordered):
        if prev_count is None or count != prev_count:
            rank = i + 1
            prev_count = count
        percent_rank = (rank - 1) / (n - 1) if n > 1 else 0.0
        if percent_rank <= 0.25:
            grades[factory] = "A"
        elif percent_rank >= 0.75:
            grades[factory] = "C"
        else:
            grades[factory] = "B"
    return grades


async def compute_factory_rankings(
    db: AsyncSession, similar_skus: list[str]
) -> dict[str, dict]:
    """
    similar_skus 집단에 속한 각 factory(제조업체)의 랭킹 점수/등급을 계산한다.

    반환: {factory: {"ranking_score": float, "top5_retailer_grade": str,
                      "import_count_grade": str, "growth_trend_grade": str}}
    """
    if not similar_skus:
        return {}

    in_params = {f"s{i}": s for i, s in enumerate(similar_skus)}
    in_clause = ", ".join(f":s{i}" for i in range(len(similar_skus)))

    # ① 국내 수입횟수: factory별 총 수입횟수 (유사 SKU 집단 내)
    count_r = await db.execute(text(f"""
        SELECT factory, SUM(import_count) AS total_import_count
        FROM sku_factory_mv
        WHERE sku_name IN ({in_clause}) AND factory IS NOT NULL
        GROUP BY factory
    """), in_params)
    counts_by_factory = {r[0]: int(r[1] or 0) for r in count_r.fetchall()}

    # ② 탑5 유통사 거래 다양성: factory별 거래한 유통사(정규화된 importer) 집합
    importers_r = await db.execute(text(f"""
        SELECT factory, array_agg(DISTINCT imp) AS all_importers
        FROM sku_factory_mv
        LEFT JOIN LATERAL unnest(importers) AS imp ON true
        WHERE sku_name IN ({in_clause}) AND factory IS NOT NULL
        GROUP BY factory
    """), in_params)
    importers_by_factory: dict[str, set[str]] = {
        r[0]: {imp for imp in (r[1] or []) if imp} for r in importers_r.fetchall()
    }

    # ③ 최근 완료된 3개년 수입횟수 (현재 연도 제외)
    cur_year = date.today().year
    y1, y2, y3 = cur_year - 3, cur_year - 2, cur_year - 1  # 시간순: 오래된 → 최근
    growth_r = await db.execute(text(f"""
        SELECT factory,
               COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = :y1 THEN 1 END)::int AS c1,
               COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = :y2 THEN 1 END)::int AS c2,
               COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = :y3 THEN 1 END)::int AS c3
        FROM import_history
        WHERE sku_name IN ({in_clause}) AND factory IS NOT NULL
        GROUP BY factory
    """), {**in_params, "y1": y1, "y2": y2, "y3": y3})
    growth_by_factory = {r[0]: (r[1], r[2], r[3]) for r in growth_r.fetchall()}

    import_count_grades = _import_count_grades(counts_by_factory)

    rankings: dict[str, dict] = {}
    for factory in counts_by_factory:
        top5_count = len(importers_by_factory.get(factory, set()) & set(TOP5_RETAILERS))
        top5_grade = _top5_grade(top5_count)

        import_grade = import_count_grades.get(factory, "C")

        gy1, gy2, gy3 = growth_by_factory.get(factory, (0, 0, 0))
        growth_grade = _growth_grade(gy1, gy2, gy3)

        weighted = (
            _GRADE_SCORE[top5_grade] * 0.5
            + _GRADE_SCORE[import_grade] * 0.3
            + _GRADE_SCORE[growth_grade] * 0.2
        )
        ranking_score = round(weighted / 3 * 100, 1)

        rankings[factory] = {
            "ranking_score": ranking_score,
            "top5_retailer_grade": top5_grade,
            "import_count_grade": import_grade,
            "growth_trend_grade": growth_grade,
        }

    return rankings
