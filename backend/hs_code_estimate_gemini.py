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

배치 처리 (무료 티어 RPD 절약): 그라운딩이 없는 순수 구조화출력 작업이라, 상품 여러 개를
한 프롬프트에 index로 구분해서 넣고 응답도 [{index, ...}, ...] 배열로 한 번에 받는다 —
Gemini 요청 "횟수"만 줄이는 것(무료 Batch API 같은 별도 기능이 아니라 그냥 프롬프트
설계)이라 RPM/RPD 절약에 직접 도움이 된다. 병목은 요청수가 아니라 TPM(분당 토큰)이 되므로
--batch-size로 상황에 맞게 조절한다(텍스트만 다루므로 원산지판독보다 훨씬 크게 잡아도 됨).

기본 모델을 gemini-3.5-flash-lite로 낮춘 것도 같은 이유 — 무료 티어 RPD가 flash(250)보다
4배 넉넉하고(1,000), 그라운딩 없는 구조화출력 작업이라 flash-lite로도 충분하다고 판단
(브랜드검증처럼 심층 리서치/그라운딩이 필요한 작업만 flash 유지).

DB에 직접 붙지 않고 백엔드 HTTP API만 호출한다(다른 크롤링 자동화 스크립트와 동일 원칙).
캐시 키(name_key)가 이미 있는 상품(hs_final_7397.csv 백필분 포함)은 재판정하지 않는다.

실행:
  GEMINI_API_KEY=... BACKEND_URL=https://sourcing-backend-ucp5.onrender.com \
    python hs_code_estimate_gemini.py --limit=300

옵션:
  --run-id=N        특정 크롤링 회차만 대상 (기본: 가장 최근 회차)
  --all-runs        모든 회차를 통틀어 대상 상품 추출 (최초 1회 소급용)
  --limit=N         이번 실행에서 판정할 상품 수 상한 (기본 300, API 비용 제어)
  --batch-size=N    프롬프트 1번에 묶을 상품 수 (기본 15)
  --top-k=N         상품당 HSK 후보 검색 상위 K개 (기본 8)
  --sleep=SEC       배치 사이 호출 간격 (기본 1초)
  --model=NAME      Gemini 모델명 (기본 gemini-3.5-flash-lite)
  --dry-run         대상 상품 목록만 출력하고 API 호출/DB 쓰기 안 함
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

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
DEFAULT_BACKEND_URL = "https://sourcing-backend-ucp5.onrender.com"

_VALID_CONFIDENCE = {"high", "medium", "very_low"}


class HsCodeBatchItemVerdict(BaseModel):
    index: int = Field(description="입력에 표시된 상품 index를 그대로 반환 (매칭용)")
    is_relevant: bool = Field(description="이 상품이 식품/생활소비재로서 실존하는 분류 가능한 물품인지. 크롤러 노이즈(완전히 무관한 카테고리, 예: 낚싯대가 식품 순위표에 낀 경우)면 false.")
    hs_code: str = Field(description="10자리 HS코드 (하이픈 포함, 예: '1509.20-0000'). is_relevant=false면 빈 문자열.")
    confidence: str = Field(description="'high'(후보 목록에서 명확히 매칭) / 'medium'(애매해서 근접 카테고리로 잠정분류) / 'very_low'(전혀 확신 없음)")
    reason: str = Field(description="판정 근거. 카테고리(product_type)와 실제 상품이 다르면 '~아님: ~로 분류' 형식으로. 한국어.")


_JUDGEMENT_CRITERIA = """
판정 기준 (HS_CODE_METHODOLOGY.md 원칙, 반드시 지킬 것):
1. 이름유사 ≠ 동일 제품. "카테고리(품목유형)"로 크롤링됐다고 실제로 그 카테고리 상품인
   건 아니다. 브랜드가 다르거나 포맷이 다르거나(냉동 vs 상온, 낱개 vs 세트), 아예 다른
   카테고리 상품이 섞여 들어오는 경우가 흔하다(크롤러 노이즈).
2. 전수 스캔 — 카테고리 대표 코드를 그대로 적용하지 말고, 각 상품명 자체가 실제로 뭔지
   하나하나 판정한다. 아래 상품들은 서로 무관하게 각자 독립적으로 판정할 것 — 한 상품의
   판정이 다른 상품에 영향을 주면 안 된다.
3. 판정은 두 갈래:
   - 진짜 무관한 상품(비식품, 크롤러 노이즈, 완전히 다른 카테고리) → is_relevant=false,
     hs_code는 빈 문자열.
   - 카테고리는 다르지만 실존하는 유사식품(예: 냉동 프렌치프라이 카테고리인데 실제로는
     감자칩/케첩/잼/라면인 경우) → is_relevant=true, 그 상품 실체에 맞는 정확한 개별
     HS코드를 그 상품에 붙어있는 후보 목록 중에서(또는 후보에 정확히 맞는 게 없으면
     알고 있는 지식으로) 골라 부여.
각 상품 아래의 "HSK 후보"는 관세청 2026 HSK 품목표에서 그 상품명과 의미가 가장 가까운
것들을 미리 검색해온 것이다 — 반드시 그 안에서만 골라야 하는 건 아니지만, 정답이 후보
안에 있는 경우가 많으니 먼저 확인할 것.

응답은 반드시 입력에 있는 상품 개수와 같은 개수의 배열로, 각 항목의 index가 입력의
index와 정확히 대응해야 한다.
""".strip()


def _batch_prompt(items: list[dict]) -> str:
    """items: [{"index":int, "product_type":str, "product_name_en":str, "candidates":[...]}]"""
    blocks = []
    for item in items:
        cand_lines = "\n".join(
            f"  - {c['hs_code']}: {c['name_en']} ({c['name_kr']})" for c in item["candidates"]
        )
        blocks.append(
            f"[상품 index={item['index']}]\n"
            f"품목유형(크롤링 카테고리): {item['product_type'] or '(미상)'}\n"
            f"실제 상품명(영어): {item['product_name_en']}\n"
            f"HSK 후보 (관세청 2026 HSK 품목표, 임베딩 유사도 상위):\n{cand_lines}"
        )
    products_text = "\n\n".join(blocks)
    return f"""당신은 관세청 HSK(품목분류) 기준으로 수입 소비재의 HS코드를 판정하는
전문가입니다. 아래 여러 상품을 각각 독립적으로 판정하세요.

{_JUDGEMENT_CRITERIA}

--- 상품 목록 ({len(items)}건) ---
{products_text}
--- 끝 ---"""


def _validate_verdict(v: HsCodeBatchItemVerdict) -> HsCodeBatchItemVerdict:
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


async def _estimate_batch(client, model: str, batch: list[tuple[str, dict]], top_k: int) -> dict[int, HsCodeBatchItemVerdict]:
    """batch: [(name_key, {"product_name_en":..., "product_type":...}), ...] (이 배치 안에서의
    순서가 곧 index). 반환: index -> HsCodeBatchItemVerdict."""
    from google.genai import types

    items = []
    for i, (_key, v) in enumerate(batch):
        candidates = _search_candidates(v["product_type"] or "", v["product_name_en"], top_k)
        items.append({
            "index": i, "product_type": v["product_type"],
            "product_name_en": v["product_name_en"], "candidates": candidates,
        })
    prompt = _batch_prompt(items)

    resp = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=list[HsCodeBatchItemVerdict],
            temperature=0,
        ),
    )
    verdicts = resp.parsed
    if verdicts is None:
        verdicts = [HsCodeBatchItemVerdict.model_validate(v) for v in json.loads(resp.text)]
    return {v.index: _validate_verdict(v) for v in verdicts}


async def _upsert_batch(http: httpx.AsyncClient, batch: list[tuple[str, dict]],
                         verdicts_by_index: dict[int, HsCodeBatchItemVerdict], model: str) -> tuple[int, int]:
    items = []
    missing = 0
    for i, (key, v) in enumerate(batch):
        verdict = verdicts_by_index.get(i)
        if verdict is None:
            missing += 1
            continue
        status = "researched_v2_direct" if verdict.is_relevant else "flagged_non_food_mismatch"
        items.append({
            "name_key": key,
            "product_name_en": v["product_name_en"],
            "product_type_hint": v["product_type"],
            "hs_code": verdict.hs_code or None,
            "confidence": verdict.confidence,
            "reason": verdict.reason,
            "evidence_url": "관세청 2026 HSK",
            "status": status,
            "estimation_source": "gemini_estimated",
            "estimation_model": model,
        })
    if items:
        resp = await http.post("/api/hs-code-estimation/upsert", json={"items": items})
        resp.raise_for_status()
    return len(items), missing


async def main(args):
    from google import genai

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY 환경변수가 필요합니다.")

    backend_url = os.getenv("BACKEND_URL", DEFAULT_BACKEND_URL)

    async with httpx.AsyncClient(base_url=backend_url, timeout=60.0) as http:
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

        batches = [targets[i:i + args.batch_size] for i in range(0, len(targets), args.batch_size)]
        ok, failed = 0, 0
        for bi, batch in enumerate(batches):
            try:
                verdicts_by_index = await _estimate_batch(client, args.model, batch, args.top_k)
                upserted, missing = await _upsert_batch(http, batch, verdicts_by_index, args.model)
                ok += upserted
                failed += missing
                print(f"  [배치 {bi+1}/{len(batches)}] {upserted}건 판정 완료"
                      + (f", {missing}건 응답 누락(다음 실행에서 재시도)" if missing else ""))
            except Exception as e:
                failed += len(batch)
                print(f"  [배치 {bi+1}/{len(batches)} 실패] {type(e).__name__}: {e} — 이 배치 {len(batch)}건은 다음 실행에서 재시도")
            time.sleep(args.sleep)

        print(f"완료: 성공 {ok}개, 실패/누락 {failed}개 (실패한 상품은 캐시에 안 남아 다음 실행에서 재시도됨)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--all-runs", action="store_true")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--batch-size", type=int, default=15)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args))
