"""
hs_code_estimate_gemini.py — 크롤링 이력에 새로 나타난 상품을 HS_CODE_METHODOLOGY.md의
"상품 하나하나 전수 스캔" 원칙 그대로 자동 판정해서 hs_code_estimation 캐시에 upsert한다.

기존에는 Claude Code 세션이 카테고리별 리뷰 파일을 사람이 훑으면서 hs_final_7397.csv를
만들었다(HS_CODE_METHODOLOGY.md 참고). 이 스크립트는 같은 판정 기준을 자동화로 옮긴 것 —
1) 로컬 임베더(local_text_embedder.py)로 관세청 2026 HSK 품목표(11,327개 HS10코드,
   reference_data/hsk_reference_20260101.csv) 중 이 상품명과 코사인 유사도가 가장 높은
   후보 K개를 뽑고, 2) Gemini 구조화 출력으로 후보 중(또는 후보 밖 자유판단으로) 최종
   HS코드를 정한다. 브랜드검증(verify_brands_gemini.py)과 달리 google_search 그라운딩은
   안 쓴다 — HS분류 근거는 웹검색이 아니라 관세청 공식 품목표 자체이므로.

원칙 (HS_CODE_METHODOLOGY.md 그대로):
  1. 이름유사 ≠ 동일 제품 — 카테고리(product_type)가 같다고 실제로 같은 상품은 아님.
  2. 전수 스캔 — 상품명 하나하나를 그 자체로 판정한다(카테고리 대표값 재사용 아님).
  3. 진짜 무관한 상품(비식품 등)은 hs_code를 비우고 status=flagged_non_food_mismatch,
     카테고리는 다르지만 실존하는 유사식품은 그 실체에 맞는 정확한 코드를 부여.

DB에 직접 붙지 않고 백엔드 HTTP API만 호출한다(다른 크롤링 자동화 스크립트와 동일 원칙).
캐시 키(name_key)가 이미 있는 상품(hs_final_7397.csv 백필분 포함)은 재판정하지 않는다.

실행:
  GEMINI_API_KEY=... BACKEND_URL=https://sourcing-backend-ucp5.onrender.com \
    python hs_code_estimate_gemini.py --limit=50

옵션:
  --run-id=N      특정 크롤링 회차만 대상 (기본: 가장 최근 회차)
  --all-runs      모든 회차를 통틀어 대상 상품 추출 (최초 1회 소급용)
  --limit=N       이번 실행에서 판정할 상품 수 상한 (기본 50, API 비용 제어)
  --top-k=N       HSK 후보 검색 상위 K개 (기본 8)
  --sleep=SEC     상품 사이 호출 간격 (기본 1초)
  --model=NAME    Gemini 모델명 (기본 gemini-2.5-flash)
  --dry-run       대상 상품 목록만 출력하고 API 호출/DB 쓰기 안 함
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

import httpx
from pydantic import BaseModel, Field

from product_name_key_normalize import normalize_product_name_key

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_BACKEND_URL = "https://sourcing-backend-ucp5.onrender.com"

_VALID_CONFIDENCE = {"high", "medium", "very_low"}
_VALID_STATUS = {"researched_v2_direct", "flagged_non_food_mismatch"}


class HsCodeVerdict(BaseModel):
    is_relevant: bool = Field(description="이 상품이 식품/생활소비재로서 실존하는 분류 가능한 물품인지. 크롤러 노이즈(완전히 무관한 카테고리, 예: 낚싯대가 식품 순위표에 낀 경우)면 false.")
    hs_code: str = Field(description="10자리 HS코드 (하이픈 포함, 예: '1509.20-0000'). is_relevant=false면 빈 문자열.")
    confidence: str = Field(description="'high'(후보 목록에서 명확히 매칭) / 'medium'(애매해서 근접 카테고리로 잠정분류) / 'very_low'(전혀 확신 없음)")
    reason: str = Field(description="판정 근거. 카테고리(product_type)와 실제 상품이 다르면 '~아님: ~로 분류' 형식으로. 한국어.")


_JUDGEMENT_CRITERIA = """
판정 기준 (HS_CODE_METHODOLOGY.md 원칙, 반드시 지킬 것):
1. 이름유사 ≠ 동일 제품. "카테고리(품목유형)"로 크롤링됐다고 실제로 그 카테고리 상품인
   건 아니다. 브랜드가 다르거나 포맷이 다르거나(냉동 vs 상온, 낱개 vs 세트), 아예 다른
   카테고리 상품이 섞여 들어오는 경우가 흔하다(크롤러 노이즈).
2. 전수 스캔 — 카테고리 대표 코드를 그대로 적용하지 말고, 이 상품명 자체가 실제로 뭔지
   판정한다.
3. 판정은 두 갈래:
   - 진짜 무관한 상품(비식품, 크롤러 노이즈, 완전히 다른 카테고리) → is_relevant=false,
     hs_code는 빈 문자열.
   - 카테고리는 다르지만 실존하는 유사식품(예: 냉동 프렌치프라이 카테고리인데 실제로는
     감자칩/케첩/잼/라면인 경우) → is_relevant=true, 그 상품 실체에 맞는 정확한 개별
     HS코드를 아래 후보 목록 중에서(또는 후보에 정확히 맞는 게 없으면 알고 있는 지식으로)
     골라 부여.
아래 후보는 관세청 2026 HSK 품목표에서 이 상품명과 의미가 가장 가까운 것들을 미리
검색해온 것이다 — 반드시 이 안에서만 골라야 하는 건 아니지만, 정답이 후보 안에 있는
경우가 많으니 먼저 확인할 것.
""".strip()


def _candidate_prompt(product_type: str, product_name_en: str, candidates: list[dict]) -> str:
    cand_lines = "\n".join(
        f"- {c['hs_code']}: {c['name_en']} ({c['name_kr']})" for c in candidates
    )
    return f"""당신은 관세청 HSK(품목분류) 기준으로 수입 소비재의 HS코드를 판정하는
전문가입니다.

품목유형(크롤링 카테고리): {product_type}
실제 상품명(영어): {product_name_en}

{_JUDGEMENT_CRITERIA}

--- HSK 후보 (관세청 2026 HSK 품목표, 임베딩 유사도 상위) ---
{cand_lines}
--- 끝 ---"""


def _validate_verdict(v: HsCodeVerdict) -> HsCodeVerdict:
    if v.confidence not in _VALID_CONFIDENCE:
        v.confidence = "very_low"
    if not v.is_relevant:
        v.hs_code = ""
    v.hs_code = (v.hs_code or "").strip()
    return v


async def _fetch_candidate_products(http: httpx.AsyncClient, run_id: int | None,
                                     all_runs: bool) -> dict[str, dict]:
    """name_key -> {"product_name_en":..., "product_type":...}"""
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
        run_ids = [runs[0]["run_id"]]

    by_key: dict[str, dict] = {}
    for rid in run_ids:
        resp = await http.get(f"/api/product-sourcing/crawl-runs/{rid}")
        resp.raise_for_status()
        for row in resp.json().get("rows", []):
            name = row.get("product_name_en")
            if not name:
                continue
            key = normalize_product_name_key(name)
            if not key or key in by_key:
                continue
            by_key[key] = {"product_name_en": name, "product_type": row.get("product_type")}
    return by_key


def _search_candidates(product_type: str, product_name_en: str, top_k: int) -> list[dict]:
    from local_text_embedder import embed_texts, cosine_top_k
    from build_hsk_reference_vectors import ensure_reference_vectors

    vectors, codes = ensure_reference_vectors()
    query_text = f"{product_name_en} ({product_type})"
    query_vec = embed_texts([query_text])[0]
    top = cosine_top_k(query_vec, vectors, top_k)
    return [
        {"hs_code": codes[i]["hs_code"], "name_kr": codes[i]["name_kr"], "name_en": codes[i]["name_en"], "score": score}
        for i, score in top
    ]


async def _estimate_one(client, model: str, product_type: str, product_name_en: str, top_k: int):
    from google.genai import types

    candidates = _search_candidates(product_type or "", product_name_en, top_k)
    prompt = _candidate_prompt(product_type or "(미상)", product_name_en, candidates)

    resp = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=HsCodeVerdict,
            temperature=0,
        ),
    )
    verdict = resp.parsed
    if verdict is None:
        verdict = HsCodeVerdict.model_validate(json.loads(resp.text))
    return _validate_verdict(verdict)


async def _upsert(http: httpx.AsyncClient, name_key: str, product_name_en: str,
                   product_type: str, verdict: HsCodeVerdict, model: str):
    status = "researched_v2_direct" if verdict.is_relevant else "flagged_non_food_mismatch"
    resp = await http.post("/api/hs-code-estimation/upsert", json={
        "items": [{
            "name_key": name_key,
            "product_name_en": product_name_en,
            "product_type_hint": product_type,
            "hs_code": verdict.hs_code or None,
            "confidence": verdict.confidence,
            "reason": verdict.reason,
            "evidence_url": "관세청 2026 HSK",
            "status": status,
            "estimation_source": "gemini_estimated",
            "estimation_model": model,
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
        by_key = await _fetch_candidate_products(http, args.run_id, args.all_runs)
        if not by_key:
            print("대상 크롤링 회차에서 상품을 찾지 못했습니다.")
            return

        resp = await http.get("/api/hs-code-estimation/keys")
        resp.raise_for_status()
        cached_keys = set(resp.json().get("name_keys", []))

        targets = [(k, v) for k, v in by_key.items() if k not in cached_keys]
        print(f"크롤링 후보 상품 {len(by_key)}개 중 미판정 {len(targets)}개")

        if args.limit:
            targets = targets[: args.limit]

        if args.dry_run:
            for key, v in targets:
                print(f"  [dry-run] {v['product_name_en']}  (key={key[:60]}, type={v['product_type']})")
            return

        # HSK 참조벡터 준비 (없으면 최초 1회 계산 — 몇 분 걸릴 수 있음, actions/cache로
        # 이후 회차부터는 스킵됨)
        from build_hsk_reference_vectors import ensure_reference_vectors
        ensure_reference_vectors()

        client = genai.Client(api_key=api_key)

        ok, failed = 0, 0
        for key, v in targets:
            try:
                verdict = await _estimate_one(client, args.model, v["product_type"], v["product_name_en"], args.top_k)
                await _upsert(http, key, v["product_name_en"], v["product_type"], verdict, args.model)
                ok += 1
                tag = verdict.hs_code if verdict.is_relevant else "(무관상품)"
                print(f"  [OK] {v['product_name_en'][:60]} → {tag} ({verdict.confidence})")
            except Exception as e:
                failed += 1
                print(f"  [FAIL] {v['product_name_en'][:60]}: {type(e).__name__}: {e}")
            time.sleep(args.sleep)

        print(f"완료: 성공 {ok}개, 실패 {failed}개 (실패한 상품은 캐시에 안 남아 다음 실행에서 재시도됨)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--all-runs", action="store_true")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args))
