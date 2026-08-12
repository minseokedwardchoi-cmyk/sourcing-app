"""
verify_brands_gemini.py — 크롤링 이력에 새로 나타난 브랜드를 Gemini API(웹검색
그라운딩)로 자동 조사해서 brand_verification 캐시에 upsert한다.

기존에는 Claude Code 세션이 WebSearch로 브랜드마다 리콜/품질·표시/법적·평판을
사람처럼 조사해서 brand_verify.csv에 쌓았다(전달 패키지 `PROMPT_브랜드검증_실행지침.md`
참고). 이 스크립트는 같은 판정 기준을 그대로 Gemini API 자동화로 옮긴 것이다 —
브랜드 검증 자체는 원래도 "AI가 검색해서 판단"하는 작업이었으므로, 도구만
Claude+WebSearch에서 Gemini+google_search 그라운딩으로 바뀌는 것뿐이다.

2단계 호출인 이유: Gemini API는 google_search 그라운딩 툴과 response_schema(구조화
출력)를 같은 호출에 함께 못 쓴다. 1단계는 그라운딩을 켜고 자유 텍스트로 리서치하고,
2단계는 그 리서치 결과를 구조화 JSON으로 변환만 한다(그라운딩 없음, 새 검색 없음).

DB에 직접 붙지 않고 백엔드 HTTP API(GET /api/product-sourcing/crawl-runs*,
GET/POST /api/brand-verification/*)만 호출한다 — 이 프로젝트의 다른 GitHub Actions
워크플로(scripts/crawl-product-sourcing 등)와 동일하게, CI 러너가 DATABASE_URL로
Postgres에 직접 붙지 않는 원칙을 그대로 따른 것. 로컬에서 직접 DB를 만지는
backfill_brand_verification.py(같은 폴더, backfill_product_sourcing_images.py와 같은
"로컬 1회성 관리 스크립트" 패턴)와는 성격이 다르다.

브랜드검증은 상품 행이 아니라 브랜드 단위 작업이라 brand_verification 캐시에
브랜드 1건당 1행만 쓴다 — 이미 캐시에 있는 브랜드는 스킵(재검증 안 함).
product_sourcing_item(메인페이지)에 이 결과를 반영하는 로직은 이 스크립트의
범위 밖이다 — 메인 테이블 승격 로직이 생기면 그쪽에서 brand_key로 이 캐시를
조회해서 채우면 된다.

먼저 backfill_brand_verification.py를 돌려서 기존 수작업 검증 데이터를 캐시로
이전해두는 걸 권장한다 — 안 하면 이미 사람이 검증해둔 브랜드까지 여기서 다시
Gemini로 조사하게 된다.

실행:
  GEMINI_API_KEY=... BACKEND_URL=https://sourcing-backend-ucp5.onrender.com \
    python verify_brands_gemini.py --source=crawl --limit=20

옵션:
  --source=crawl        product_sourcing_crawl_snapshot_item에서 브랜드 후보 추출 (기본)
  --run-id=N              특정 크롤링 회차만 대상 (기본: 가장 최근 회차)
  --all-runs               모든 회차를 통틀어 대상 브랜드 추출 (최초 1회 소급용)
  --limit=N                이번 실행에서 검증할 브랜드 수 상한 (기본 20, API 비용 제어)
  --sleep=SEC              브랜드 사이 호출 간격 (기본 2초, 레이트리밋 방지)
  --model=NAME              Gemini 모델명 (기본 gemini-2.5-flash)
  --dry-run                 대상 브랜드 목록만 출력하고 API 호출/DB 쓰기 안 함
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, Field

from brand_key_normalize import normalize_brand_key

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_BACKEND_URL = "https://sourcing-backend-ucp5.onrender.com"

_VALID_STATUS = {"통과", "탈락"}
_VALID_FIVE_YEAR = {"O", "X", "-"}


class BrandVerdict(BaseModel):
    recall_status: str = Field(description="리콜 이력 판정. 반드시 '통과' 또는 '탈락' 중 하나.")
    quality_label_status: str = Field(description="품질·표시(원산지·등급 등 허위표시) 판정. 반드시 '통과' 또는 '탈락' 중 하나.")
    legal_risk_status: str = Field(description="법적·평판 리스크(집단소송/규제 벌금/반독점 등) 판정. 반드시 '통과' 또는 '탈락' 중 하나.")
    five_year_issue: str = Field(description="탈락 사유 중 가장 최근 건이 5년 이내면 'O', 그보다 오래면 'X', 전부 통과면 '-'.")
    notes: str = Field(description="판정 근거를 1~3개, 사건명·연도 포함해서 한국어로 짧게 요약. 특이사항 없으면 '검색결과 특이사항 없음'.")


_JUDGEMENT_CRITERIA = """
판정 기준 (반드시 지킬 것):
- "확인필요"는 없다. 뚜렷한 부정적 근거(공식 리콜, 소송 패소/합의, 규제기관 제재)가
  검색으로 안 나오면 기본적으로 각 항목 '통과' 처리한다.
- 리콜 이력: 최근 리콜(자발적 회수 포함) 이력이 있으면 '탈락'. 소소한 이물질 혼입
  리콜 1건 정도도 리콜 항목만 '탈락' 처리하고, 품질·표시/법적·평판은 별개로 판단한다.
- 품질·표시: 원산지·등급·성분 등에 대한 허위표시로 소송/제재를 받은 이력이 있으면 '탈락'.
- 법적·평판 리스크: 위 두 가지 외의 집단소송 패소/합의, 규제기관 벌금, 반독점 이슈 등이
  있으면 '탈락'.
- 해당 브랜드의 계열사나 관련 없는 다른 제품 라인에서 발생한 이슈는, 이번에 검증 중인
  브랜드/제품과 직접 관련이 없다면 무관 처리하고 통과시키되, notes에 "~와는 무관"이라고
  명시한다.
- five_year_issue는 '탈락' 처리된 사유들 중 가장 최근 사건의 연도를 기준으로 판단한다
  (전부 통과면 '-').
- notes는 반드시 근거(연도, 사건명, 소송명 등)를 짧게라도 남긴다. "그냥 통과"처럼
  근거 없는 문장은 쓰지 않는다.
""".strip()


def _research_prompt(brand: str, samples: list[str], today: str) -> str:
    sample_txt = "; ".join(samples[:3]) if samples else "(샘플 상품명 없음)"
    return f"""당신은 식품/소비재 브랜드의 리콜·품질표시·법적평판 이슈를 조사하는
리서처입니다. 아래 브랜드에 대해 웹 검색으로 조사하세요.

브랜드명: {brand}
이 브랜드의 실제 판매 상품 예시: {sample_txt}
오늘 날짜: {today}

다음 3가지 축을 각각 조사하고, 찾은 근거(사건명, 연도, 출처)를 구체적으로 정리해서
자유 텍스트로 답하세요 (아직 JSON으로 만들지 마세요):
1. 리콜 이력 (recall) — 최근 리콜/자발적 회수 이력
2. 품질·표시 (quality/labeling) — 원산지·등급·성분 허위표시 관련 소송/제재
3. 법적·평판 리스크 (legal/reputation) — 그 외 집단소송, 규제 벌금, 반독점 등

{_JUDGEMENT_CRITERIA}

브랜드명만으로 검색하지 말고, 위 상품 예시를 참고해서 정확히 이 브랜드(동명이인 브랜드가
아닌지)를 특정한 뒤 조사하세요. 찾은 내용이 없으면 "검색결과 특이사항 없음"이라고
명시하세요."""


def _structuring_prompt(research_text: str) -> str:
    return f"""아래는 어떤 브랜드에 대한 리콜/품질·표시/법적·평판 리서치 결과입니다.
이 내용을 바탕으로 최종 판정을 구조화된 형식으로 정리하세요. 새로 검색하지 말고
아래 리서치 내용에만 근거해서 판단하세요.

{_JUDGEMENT_CRITERIA}

--- 리서치 결과 ---
{research_text}
--- 끝 ---"""


async def _fetch_candidate_brands(http: httpx.AsyncClient, run_id: int | None,
                                   all_runs: bool) -> dict[str, list[str]]:
    run_ids: list[int]
    if all_runs:
        resp = await http.get("/api/product-sourcing/crawl-runs")
        resp.raise_for_status()
        run_ids = [r["run_id"] for r in resp.json().get("runs", [])]
    elif run_id is not None:
        run_ids = [run_id]
    else:
        resp = await http.get("/api/product-sourcing/crawl-runs")
        resp.raise_for_status()
        runs = resp.json().get("runs", [])
        if not runs:
            return {}
        run_ids = [runs[0]["run_id"]]  # run_at 내림차순 정렬되어 첫 항목이 최신

    by_brand: dict[str, list[str]] = {}
    for rid in run_ids:
        resp = await http.get(f"/api/product-sourcing/crawl-runs/{rid}")
        resp.raise_for_status()
        for row in resp.json().get("rows", []):
            brand = row.get("brand")
            if not brand:
                continue
            by_brand.setdefault(brand, [])
            name = row.get("product_name_en")
            if name and name not in by_brand[brand]:
                by_brand[brand].append(name)
    return by_brand


async def _already_cached_keys(http: httpx.AsyncClient) -> set[str]:
    resp = await http.get("/api/brand-verification/keys")
    resp.raise_for_status()
    return set(resp.json().get("brand_keys", []))


def _validate_verdict(v: BrandVerdict) -> BrandVerdict:
    if v.recall_status not in _VALID_STATUS:
        v.recall_status = "탈락" if "탈락" in v.recall_status else "통과"
    if v.quality_label_status not in _VALID_STATUS:
        v.quality_label_status = "탈락" if "탈락" in v.quality_label_status else "통과"
    if v.legal_risk_status not in _VALID_STATUS:
        v.legal_risk_status = "탈락" if "탈락" in v.legal_risk_status else "통과"
    if v.five_year_issue not in _VALID_FIVE_YEAR:
        v.five_year_issue = "-"
    return v


async def _verify_one_brand(client, model: str, brand: str, samples: list[str]):
    """(BrandVerdict, sources) 반환. 실패하면 예외를 던진다(호출부에서 브랜드별로 catch)."""
    from google.genai import types

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    research_resp = await client.aio.models.generate_content(
        model=model,
        contents=_research_prompt(brand, samples, today),
        config=types.GenerateContentConfig(tools=[grounding_tool], temperature=0.2),
    )
    research_text = research_resp.text or ""

    sources = []
    try:
        chunks = research_resp.candidates[0].grounding_metadata.grounding_chunks or []
        for c in chunks:
            if getattr(c, "web", None) and c.web.uri:
                sources.append({"title": c.web.title, "url": c.web.uri})
    except Exception:
        pass

    structure_resp = await client.aio.models.generate_content(
        model=model,
        contents=_structuring_prompt(research_text),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=BrandVerdict,
            temperature=0,
        ),
    )
    verdict = structure_resp.parsed
    if verdict is None:
        verdict = BrandVerdict.model_validate(json.loads(structure_resp.text))
    verdict = _validate_verdict(verdict)
    return verdict, sources


async def _upsert(http: httpx.AsyncClient, brand_key: str, brand_display: str,
                   verdict: BrandVerdict, sources: list[dict], model: str):
    resp = await http.post("/api/brand-verification/upsert", json={
        "items": [{
            "brand_key": brand_key,
            "brand_display": brand_display,
            "recall_status": verdict.recall_status,
            "quality_label_status": verdict.quality_label_status,
            "legal_risk_status": verdict.legal_risk_status,
            "five_year_issue": verdict.five_year_issue,
            "notes": verdict.notes,
            "sources": sources,
            "verification_model": model,
        }]
    })
    resp.raise_for_status()


async def main(args):
    from google import genai

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY 환경변수가 필요합니다.")

    backend_url = os.getenv("BACKEND_URL", DEFAULT_BACKEND_URL)

    async with httpx.AsyncClient(base_url=backend_url, timeout=30.0) as http:
        by_brand = await _fetch_candidate_brands(http, args.run_id, args.all_runs)
        if not by_brand:
            print("대상 크롤링 회차에서 브랜드를 찾지 못했습니다.")
            return

        cached_keys = await _already_cached_keys(http)

        targets = []  # (brand_key, brand_display, samples)
        seen_keys = set()
        for brand, samples in by_brand.items():
            key = normalize_brand_key(brand)
            if not key or key in cached_keys or key in seen_keys:
                continue
            seen_keys.add(key)
            targets.append((key, brand, samples))

        print(f"크롤링 후보 브랜드 {len(by_brand)}개 중 미검증 {len(targets)}개")

        if args.limit:
            targets = targets[: args.limit]

        if args.dry_run:
            for key, brand, samples in targets:
                print(f"  [dry-run] {brand}  (key={key}, samples={samples[:2]})")
            return

        client = genai.Client(api_key=api_key)

        ok, failed = 0, 0
        for key, brand, samples in targets:
            try:
                verdict, sources = await _verify_one_brand(client, args.model, brand, samples)
                await _upsert(http, key, brand, verdict, sources, args.model)
                ok += 1
                print(f"  [OK] {brand} → 리콜:{verdict.recall_status} 품질:{verdict.quality_label_status} "
                      f"법적:{verdict.legal_risk_status} 5년이내:{verdict.five_year_issue}")
            except Exception as e:
                failed += 1
                print(f"  [FAIL] {brand}: {type(e).__name__}: {e}")
            time.sleep(args.sleep)

        print(f"완료: 성공 {ok}개, 실패 {failed}개 (실패한 브랜드는 캐시에 안 남아 다음 실행에서 재시도됨)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["crawl"], default="crawl")
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--all-runs", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args))
