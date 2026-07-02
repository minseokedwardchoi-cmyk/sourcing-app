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


async def _compute_rankings_for_scope(
    db: AsyncSession, scope_sql: str, scope_params: dict, key_col: str = "factory"
) -> dict[str, dict]:
    """
    scope_sql(예: "sku_name IN (...)" 또는 "country = :country")로 한정된 집단 안에서
    key_col(factory 또는 manufacturer) 단위 랭킹 점수/등급을 계산하는 공용 로직.
    """
    count_r = await db.execute(text(f"""
        SELECT {key_col}, SUM(import_count) AS total_import_count
        FROM sku_factory_mv
        WHERE {scope_sql} AND {key_col} IS NOT NULL
        GROUP BY {key_col}
    """), scope_params)
    counts_by_key = {r[0]: int(r[1] or 0) for r in count_r.fetchall()}

    importers_r = await db.execute(text(f"""
        SELECT {key_col}, array_agg(DISTINCT imp) AS all_importers
        FROM sku_factory_mv
        LEFT JOIN LATERAL unnest(importers) AS imp ON true
        WHERE {scope_sql} AND {key_col} IS NOT NULL
        GROUP BY {key_col}
    """), scope_params)
    importers_by_key: dict[str, set[str]] = {
        r[0]: {imp for imp in (r[1] or []) if imp} for r in importers_r.fetchall()
    }

    cur_year = date.today().year
    y1, y2, y3 = cur_year - 3, cur_year - 2, cur_year - 1  # 시간순: 오래된 → 최근
    growth_r = await db.execute(text(f"""
        SELECT {key_col},
               COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = :y1 THEN 1 END)::int AS c1,
               COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = :y2 THEN 1 END)::int AS c2,
               COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = :y3 THEN 1 END)::int AS c3
        FROM import_history
        WHERE {scope_sql} AND {key_col} IS NOT NULL
        GROUP BY {key_col}
    """), {**scope_params, "y1": y1, "y2": y2, "y3": y3})
    growth_by_key = {r[0]: (r[1], r[2], r[3]) for r in growth_r.fetchall()}

    import_count_grades = _import_count_grades(counts_by_key)

    rankings: dict[str, dict] = {}
    for key in counts_by_key:
        matched_top5 = sorted(
            importers_by_key.get(key, set()) & set(TOP5_RETAILERS),
            key=TOP5_RETAILERS.index,
        )
        top5_grade = _top5_grade(len(matched_top5))

        import_grade = import_count_grades.get(key, "C")

        gy1, gy2, gy3 = growth_by_key.get(key, (0, 0, 0))
        growth_grade = _growth_grade(gy1, gy2, gy3)

        weighted = (
            _GRADE_SCORE[top5_grade] * 0.5
            + _GRADE_SCORE[import_grade] * 0.3
            + _GRADE_SCORE[growth_grade] * 0.2
        )
        ranking_score = round(weighted / 3 * 100, 1)

        rankings[key] = {
            "ranking_score": ranking_score,
            "top5_retailer_grade": top5_grade,
            "top5_retailers_matched": matched_top5,
            "import_count_grade": import_grade,
            "total_import_count": counts_by_key.get(key, 0),
            "growth_trend_grade": growth_grade,
            "growth_yearly": [
                {"year": str(y1), "count": gy1},
                {"year": str(y2), "count": gy2},
                {"year": str(y3), "count": gy3},
            ],
        }

    return rankings


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

    return await _compute_rankings_for_scope(
        db, f"sku_name IN ({in_clause})", in_params, key_col="factory"
    )


async def compute_factory_ranking_per_sku(
    db: AsyncSession, factory: str, skus: list[str]
) -> dict[str, dict]:
    """
    제조사 상세 페이지: 한 factory가 취급하는 여러 SKU 각각에 대해, 그 SKU를
    취급하는 factory 집단(peer group) 안에서의 상대 랭킹을 계산한다.

    compute_factory_rankings(db, [sku_name])를 SKU마다 반복 호출하면 SKU 수만큼
    쿼리가 늘어나므로(N+1), sku_name/factory로 그룹핑한 세 번의 쿼리로 모든 SKU의
    peer group 데이터를 한 번에 가져온 뒤 파이썬에서 SKU별로 등급을 계산한다.
    peer group 산정 방식(등급/가중치 로직)은 compute_factory_rankings와 동일하다.

    반환: {sku_name: {"ranking_score": float}} — 요청한 factory 관점의 점수만 담는다.
    """
    if not skus:
        return {}

    in_params = {f"s{i}": s for i, s in enumerate(skus)}
    in_clause = ", ".join(f":s{i}" for i in range(len(skus)))

    count_r = await db.execute(text(f"""
        SELECT sku_name, factory, SUM(import_count) AS cnt
        FROM sku_factory_mv
        WHERE sku_name IN ({in_clause}) AND factory IS NOT NULL
        GROUP BY sku_name, factory
    """), in_params)
    counts_by_sku: dict[str, dict[str, int]] = {}
    for sku_name, fac, cnt in count_r.fetchall():
        counts_by_sku.setdefault(sku_name, {})[fac] = int(cnt or 0)

    importers_r = await db.execute(text(f"""
        SELECT sku_name, factory, array_agg(DISTINCT imp) AS all_importers
        FROM sku_factory_mv
        LEFT JOIN LATERAL unnest(importers) AS imp ON true
        WHERE sku_name IN ({in_clause}) AND factory IS NOT NULL
        GROUP BY sku_name, factory
    """), in_params)
    importers_by_sku_factory: dict[tuple[str, str], set[str]] = {
        (sku_name, fac): {imp for imp in (imps or []) if imp}
        for sku_name, fac, imps in importers_r.fetchall()
    }

    cur_year = date.today().year
    y1, y2, y3 = cur_year - 3, cur_year - 2, cur_year - 1
    growth_r = await db.execute(text(f"""
        SELECT sku_name, factory,
               COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = :y1 THEN 1 END)::int,
               COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = :y2 THEN 1 END)::int,
               COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = :y3 THEN 1 END)::int
        FROM import_history
        WHERE sku_name IN ({in_clause}) AND factory IS NOT NULL
        GROUP BY sku_name, factory
    """), {**in_params, "y1": y1, "y2": y2, "y3": y3})
    growth_by_sku_factory: dict[tuple[str, str], tuple[int, int, int]] = {
        (sku_name, fac): (c1, c2, c3) for sku_name, fac, c1, c2, c3 in growth_r.fetchall()
    }

    results: dict[str, dict] = {}
    for sku_name, counts_by_factory in counts_by_sku.items():
        if factory not in counts_by_factory:
            continue
        import_grade = _import_count_grades(counts_by_factory).get(factory, "C")

        matched_top5 = sorted(
            importers_by_sku_factory.get((sku_name, factory), set()) & set(TOP5_RETAILERS),
            key=TOP5_RETAILERS.index,
        )
        top5_grade = _top5_grade(len(matched_top5))

        gy1, gy2, gy3 = growth_by_sku_factory.get((sku_name, factory), (0, 0, 0))
        growth_grade = _growth_grade(gy1, gy2, gy3)

        weighted = (
            _GRADE_SCORE[top5_grade] * 0.5
            + _GRADE_SCORE[import_grade] * 0.3
            + _GRADE_SCORE[growth_grade] * 0.2
        )
        results[sku_name] = {"ranking_score": round(weighted / 3 * 100, 1)}

    return results


async def compute_manufacturer_rankings_by_country(
    db: AsyncSession, country: str
) -> dict[str, dict]:
    """
    국가 페이지: 해당 country에 속한 제조사(manufacturer) 집단 안에서 동일한
    랭킹 로직(가중치/등급 산출)을 재사용해 점수를 계산한다. (새 점수 로직 아님)

    반환: {manufacturer: {...}} (compute_factory_rankings와 동일한 필드 구조)
    """
    return await _compute_rankings_for_scope(
        db, "country = :country", {"country": country}, key_col="manufacturer"
    )


async def compute_best_sku_rankings_for_country(
    db: AsyncSession, country: str
) -> dict[str, dict]:
    """
    국가 페이지: 각 제조사(manufacturer)에 대해 SKU별로 평가한 점수 중 가장 높은 점수와
    해당 SKU명을 반환한다. import_count_grade는 국가에 관계없이 해당 SKU를 취급하는
    전체 제조사 집단 내 상대 순위로 산출한다(같은 SKU라면 다른 국가 제조사도 비교
    대상에 포함). top5/growth grade는 제조사 전체 이력 기준으로 산출한다.

    반환: {manufacturer: {ranking_score, best_sku_name, top5_retailer_grade,
                          top5_retailers_matched, import_count_grade, total_import_count,
                          growth_trend_grade, growth_yearly}}
    """
    # 1. Per (manufacturer, sku_name) import counts — 국가 페이지에 표시할 실적 수치는
    #    해당 국가로 한정한다.
    count_r = await db.execute(text("""
        SELECT manufacturer, sku_name, SUM(import_count) AS cnt
        FROM sku_factory_mv
        WHERE country = :country AND manufacturer IS NOT NULL
        GROUP BY manufacturer, sku_name
    """), {"country": country})

    mfr_sku_counts: dict[str, dict[str, int]] = {}
    sku_names: set[str] = set()
    for mfr_key, sku_name, cnt in count_r.fetchall():
        mfr_sku_counts.setdefault(mfr_key, {})[sku_name] = int(cnt or 0)
        sku_names.add(sku_name)

    if not mfr_sku_counts:
        return {}

    # 2. import_count_grade의 비교 대상(peer group)은 국가로 한정하지 않고, 같은 SKU를
    #    취급하는 전체 제조사(모든 국가)로 잡는다.
    sku_in_params = {f"sk{i}": s for i, s in enumerate(sku_names)}
    sku_in_clause = ", ".join(f":sk{i}" for i in range(len(sku_names)))
    sku_all_counts_r = await db.execute(text(f"""
        SELECT sku_name, manufacturer, SUM(import_count) AS cnt
        FROM sku_factory_mv
        WHERE sku_name IN ({sku_in_clause}) AND manufacturer IS NOT NULL
        GROUP BY sku_name, manufacturer
    """), sku_in_params)
    sku_all_counts: dict[str, dict[str, int]] = {}
    for sku_name, mfr_key, cnt in sku_all_counts_r.fetchall():
        sku_all_counts.setdefault(sku_name, {})[mfr_key] = int(cnt or 0)

    sku_import_grades: dict[str, dict[str, str]] = {
        sku_name: _import_count_grades(counts)
        for sku_name, counts in sku_all_counts.items()
    }

    # 3. Top5 importers per manufacturer (country scope)
    importers_r = await db.execute(text("""
        SELECT manufacturer, array_agg(DISTINCT imp) AS all_importers
        FROM sku_factory_mv
        LEFT JOIN LATERAL unnest(importers) AS imp ON true
        WHERE country = :country AND manufacturer IS NOT NULL
        GROUP BY manufacturer
    """), {"country": country})
    importers_by_mfr: dict[str, set[str]] = {
        r[0]: {imp for imp in (r[1] or []) if imp}
        for r in importers_r.fetchall()
    }

    # 4. Growth per manufacturer (country scope)
    cur_year = date.today().year
    y1, y2, y3 = cur_year - 3, cur_year - 2, cur_year - 1
    growth_r = await db.execute(text("""
        SELECT manufacturer,
               COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = :y1 THEN 1 END)::int,
               COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = :y2 THEN 1 END)::int,
               COUNT(CASE WHEN EXTRACT(YEAR FROM COALESCE(import_date, process_date)) = :y3 THEN 1 END)::int
        FROM import_history
        WHERE country = :country AND manufacturer IS NOT NULL
        GROUP BY manufacturer
    """), {"country": country, "y1": y1, "y2": y2, "y3": y3})
    growth_by_mfr = {r[0]: (r[1], r[2], r[3]) for r in growth_r.fetchall()}

    # 5. For each manufacturer, find the best-scoring SKU
    results: dict[str, dict] = {}
    for mfr_key, sku_counts_map in mfr_sku_counts.items():
        matched_top5 = sorted(
            importers_by_mfr.get(mfr_key, set()) & set(TOP5_RETAILERS),
            key=TOP5_RETAILERS.index,
        )
        top5_grade = _top5_grade(len(matched_top5))
        gy1, gy2, gy3 = growth_by_mfr.get(mfr_key, (0, 0, 0))
        growth_grade = _growth_grade(gy1, gy2, gy3)

        best_score: float | None = None
        best_sku: str | None = None
        best_sku_count = 0
        best_import_grade = "C"

        for sku_name, sku_count in sku_counts_map.items():
            import_grade = sku_import_grades.get(sku_name, {}).get(mfr_key, "C")
            weighted = (
                _GRADE_SCORE[top5_grade] * 0.5
                + _GRADE_SCORE[import_grade] * 0.3
                + _GRADE_SCORE[growth_grade] * 0.2
            )
            score = round(weighted / 3 * 100, 1)
            if best_score is None or score > best_score:
                best_score = score
                best_sku = sku_name
                best_sku_count = sku_count
                best_import_grade = import_grade

        results[mfr_key] = {
            "ranking_score":          best_score,
            "best_sku_name":          best_sku,
            "top5_retailer_grade":    top5_grade,
            "top5_retailers_matched": matched_top5,
            "import_count_grade":     best_import_grade,
            "total_import_count":     best_sku_count,
            "growth_trend_grade":     growth_grade,
            "growth_yearly": [
                {"year": str(y1), "count": gy1},
                {"year": str(y2), "count": gy2},
                {"year": str(y3), "count": gy3},
            ],
        }

    return results
