"""
verify_origin_gemini.py — 크롤링 이력에 새로 나타난 상품의 원산지를 Gemini 비전
API로 자동 판독해서 product_origin_verification 캐시에 upsert한다.

기존엔 사람이 상품 패키지 사진(정면/후면/측면 2~8장)을 직접 보고 "Product of ~",
"Made in ~" 같은 문구를 찾아 기록했다(zip 전달 패키지 `원산지_판독_방법_INSTRUCTIONS.md`).
이 스크립트는 같은 판정 기준을 Gemini 비전 API로 자동화한 것이다.

브랜드검증과 다른 점: 원산지는 브랜드 단위로 재사용 못한다(같은 브랜드도 유통사·용량마다
원산지가 다를 수 있음 — zip 지침의 QC 경고). 그래서 캐시 키가 브랜드가 아니라 **상품
URL**이다. 그리고 그라운딩(실시간 검색)이 필요 없다 — 원산지 표시 규정 같은 건 자주
안 바뀌는 정적 지식이라, 이미지+텍스트를 한 번의 Gemini 호출로 구조화 출력까지 바로
받는다(브랜드검증처럼 그라운딩 vs 구조화출력 충돌 문제로 2단계 호출할 필요가 없음).

배치 처리 (무료 티어 RPD 절약): HS코드 추정과 같은 이유로 여러 상품을 한 프롬프트에
index로 구분해서 묶어 보낸다. 다만 이미지가 토큰을 많이 먹어서(각 상품 최대 4장) TPM이
HS코드보다 훨씬 빨리 병목이 되므로 --batch-size 기본값을 작게(4) 잡는다 — 배치 4개 x
이미지 4장 = 최대 16장/요청 정도가 무난한 상한.

기본 모델을 gemini-2.5-flash-lite로 낮춘 것도 같은 이유(무료 티어 RPD가 flash보다 4배
넉넉함, 그라운딩 없는 구조화출력 작업이라 flash-lite로도 충분하다고 판단).

사진 확보 전략 (유통사별로 다름):
  - 크롤러가 image_urls(여러 장)를 이미 보내준 경우 → 그대로 사용.
  - 없고 retailer가 amazon/aeon이면 → 이 스크립트가 상품 상세페이지 URL을 r.jina.ai
    리더 프록시로 다시 읽어서(검색결과가 아니라 상세페이지라 갤러리에 후면/측면 사진이
    더 있을 가능성이 높음) 이미지를 추가로 추출한다. r.jina.ai 자체 캐시 문제(예전에
    이 프로젝트에서 실제로 겪음 — 오래된 스냅샷을 계속 돌려줌) 회피를 위해 매번 URL에
    랜덤 타임스탬프를 붙여 요청한다.
  - 그 외(월마트/샘스클럽처럼 봇차단으로 상세페이지 자체 접근이 막혀있을 가능성이 높은
    경우) → 크롤링 당시 저장된 썸네일 1장만 사용(또는 아예 없으면 사진 없이 텍스트
    추정만 시도). 월마트/샘스클럽이 상세페이지 방문 시 갤러리 사진을 image_urls로
    같이 보내주면 자동으로 이 경로를 안 타고 더 정확해진다 — HANDOFF 문서 참고.

실행:
  GEMINI_API_KEY=... BACKEND_URL=https://sourcing-backend-ucp5.onrender.com \
    python verify_origin_gemini.py --limit=100

옵션은 hs_code_estimate_gemini.py와 동일한 구조 (--run-id/--all-runs/--limit/
--batch-size/--sleep/--model/--dry-run).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import time
from datetime import datetime

import httpx
from pydantic import BaseModel, Field

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
DEFAULT_BACKEND_URL = "https://sourcing-backend-ucp5.onrender.com"

MAX_IMAGES_PER_PRODUCT = 4
IMAGE_FETCH_TIMEOUT = 12.0
JINA_FETCH_TIMEOUT = 20.0

_VALID_ORIGIN_FOUND = {"Y", "E", "N"}

_IMG_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_SKIP_IMAGE_HINTS = ("sprite", "icon", "logo", "1x1", "pixel", "spinner", "loading", "blank.gif")


class OriginBatchItemVerdict(BaseModel):
    index: int = Field(description="입력에 표시된 상품 index를 그대로 반환 (매칭용)")
    origin_found: str = Field(description="사진에서 실제 문구를 찾았으면 'Y', 문구는 없지만 추정 가능하면 'E', 근거가 아예 없으면 'N'")
    origin_text: str = Field(description="Y면 라벨 문구 그대로(또는 요약), E면 '국가명(추정)' 형식, N이면 빈 문자열")
    note: str = Field(description="판정 근거·특이사항 (한국어). E일 때는 추정 근거를 반드시 남길 것")


_JUDGEMENT_RULES = """
판정 기준 (각 상품에 개별 적용, 반드시 순서대로):
1. 그 상품에 딸린 사진에서 "Product of ~", "Made in ~", "Manufactured in ~",
   "Packed in ~", "Imported from ~" 같은 원산지 표시 문구를 먼저 찾는다.
   영양정보/성분표만 보이는 사진은 무시하고 포장 전체가 보이는 사진을 우선한다.
   여러 나라가 같이 언급되면("Product of Italy, Spain, Greece" 등) 있는 그대로
   기록하고 억지로 한 나라로 단정하지 않는다. 문구를 실제로 찾았으면
   origin_found="Y", origin_text에 그 문구(또는 짧은 요약)를 적는다. 일본
   상품이면 "国内製造"(국내제조) 표기도 실측 문구로 인정한다.
2. 문구를 못 찾았거나(사진에 없음), 사진 자체가 아예 없으면 아래 추정 규칙을
   적용해서 origin_found="E"로 표시하고 origin_text에 "국가명(추정)" 형식으로
   적는다. 각 상품에 명시된 "유통사국가"를 기준으로:
   - 유통사국가가 미국이면: 미국 원산지표시법(COOL)상 가공식품(마요네즈·크래커·
     시리얼처럼 원재료를 가공한 식품)은 원산지 표시 의무 자체가 없다. 브랜드가
     미국 내수 브랜드/가공식품으로 보이고 딱히 수입 근거가 없으면 "미국(추정)"
     으로 적고 note에 "가공식품 COOL 적용제외 - 미국 내수 브랜드로 추정"이라고
     남긴다. 다른 근거(브랜드 원산지가 널리 알려진 경우 등)로 실제 원산지가
     짐작되면 그 나라 + "(추정)" + note에 근거.
   - 유통사국가가 일본이면: 일본은 가공식품도 원산지 표시가 원칙적으로 필요해서
     미국식 예외가 성립하지 않는다. 이 브랜드가 일본 내수 제조업체의 자체상품이고,
     이 상품(원료)을 일본이 상업적으로 거의 생산하지 않는다는 게 일반 상식이면
     (기후·지리상 재배/생산이 없는 작물·원료), 정확한 원산지국은 불명이지만
     원료를 해외에서 수입해 일본에서 가공했을 가능성이 높다는 뜻으로 "다국적
     블렌드(추정)"라고 적고 note에 그 판단 근거(어떤 원료가 일본에서 생산 안
     되는지)를 남긴다.
   - 유통사국가가 말레이시아면: 이 브랜드가 여러 나라 유통사에서도 팔리는
     글로벌 브랜드로 보이면, 그 브랜드에 일반적으로 알려진 원산지를 근거로
     추정한다(note에 "글로벌 브랜드로 알려진 원산지 기준 추정"이라고 남길 것).
     말레이시아 로컬 브랜드면 일본과 같은 논리(말레이시아가 이 원료를 상업
     생산하지 않으면 수입 추정)를 적용한다.
3. 억지로 추정하지 말 것 — 위 규칙으로도 근거가 없으면 origin_found="N"(확인불가)
   으로 정직하게 남기고 origin_text는 빈 문자열로 둔다. 틀린 추정보다 "모른다"가 낫다.
4. "확인필요"라는 애매한 값은 절대 쓰지 않는다 — Y/E/N 중 하나만 쓴다.
5. 사진이 하나도 제공되지 않은 상품이면 1번은 건너뛰고 바로 2번(추정) 또는 3번
   (확인불가)을 적용한다 — 이 경우 note에 "사진 없음, 텍스트 정보만으로 판단"
   이라고 남길 것.
6. 아래 상품들은 서로 무관하게 각자 독립적으로 판정한다 — 한 상품의 판정이나
   사진이 다른 상품 판정에 영향을 주면 안 된다. 응답은 입력 상품 개수와 같은
   개수의 배열로, 각 항목의 index가 입력의 index와 정확히 대응해야 한다.
""".strip()


def _retailer_country(retailer: str, source_site: str | None) -> str:
    if retailer == "aeon":
        if source_site == "aeon-my":
            return "말레이시아"
        return "일본"
    return "미국"  # walmart / samsclub / amazon


def _product_block(index: int, row: dict, retailer_country: str, image_count: int) -> str:
    return (
        f"[상품 index={index}]\n"
        f"상품유형: {row.get('product_type') or '(정보없음)'}\n"
        f"브랜드: {row.get('brand') or '(정보없음)'}\n"
        f"상품명(원어): {row.get('product_name_en') or '(정보없음)'}\n"
        f"유통사: {row['retailer']} (유통사국가: {retailer_country})\n"
        f"첨부 사진 수: {image_count}장 (바로 아래에 이어서 첨부됨, 이 상품 것만)"
    )


def _guess_mime(url: str, content_type: str | None) -> str:
    if content_type and content_type.startswith("image/"):
        return content_type.split(";")[0].strip()
    lower = url.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


async def _fetch_extra_images_via_jina(http: httpx.AsyncClient, product_url: str) -> list[str]:
    """아마존/이온몰 상세페이지를 r.jina.ai로 다시 읽어서 이미지 URL 후보를 뽑는다.
    실패하면 빈 리스트 반환(호출부가 기존 썸네일만으로 계속 진행)."""
    sep = "&" if "?" in product_url else "?"
    cache_busted = f"{product_url}{sep}_t={int(time.time())}{random.randint(1000, 9999)}"
    jina_url = f"https://r.jina.ai/{cache_busted}"
    try:
        resp = await http.get(jina_url, timeout=JINA_FETCH_TIMEOUT)
        resp.raise_for_status()
        markdown = resp.text
    except Exception:
        return []

    urls = []
    for m in _IMG_MARKDOWN_RE.finditer(markdown):
        u = m.group(1)
        low = u.lower()
        if any(hint in low for hint in _SKIP_IMAGE_HINTS):
            continue
        if u not in urls:
            urls.append(u)
        if len(urls) >= MAX_IMAGES_PER_PRODUCT:
            break
    return urls


async def _collect_images(http: httpx.AsyncClient, row: dict) -> list[str]:
    if row.get("image_urls"):
        return list(row["image_urls"])[:MAX_IMAGES_PER_PRODUCT]

    urls = []
    if row.get("image_url"):
        urls.append(row["image_url"])

    if row["retailer"] in ("amazon", "aeon") and row.get("url"):
        extra = await _fetch_extra_images_via_jina(http, row["url"])
        for u in extra:
            if u not in urls:
                urls.append(u)
            if len(urls) >= MAX_IMAGES_PER_PRODUCT:
                break

    return urls[:MAX_IMAGES_PER_PRODUCT]


async def _download_images(http: httpx.AsyncClient, urls: list[str]) -> list[tuple[bytes, str, str]]:
    """(bytes, mime, url) 목록. 개별 실패는 건너뛴다."""
    out = []
    for u in urls:
        try:
            resp = await http.get(u, timeout=IMAGE_FETCH_TIMEOUT)
            resp.raise_for_status()
            mime = _guess_mime(u, resp.headers.get("content-type"))
            out.append((resp.content, mime, u))
        except Exception:
            continue
    return out


def _validate_verdict(v: OriginBatchItemVerdict) -> OriginBatchItemVerdict:
    if v.origin_found not in _VALID_ORIGIN_FOUND:
        v.origin_found = "N"
        v.origin_text = ""
    if v.origin_found == "N":
        v.origin_text = ""
    return v


async def _verify_batch(client, model: str, batch: list[tuple[dict, list[tuple[bytes, str, str]]]]):
    """batch: [(row, images), ...] (이 배치 안에서의 순서가 곧 index).
    반환: index -> OriginBatchItemVerdict."""
    from google.genai import types

    parts = [types.Part.from_text(text=(
        "당신은 식품 패키지 사진에서 원산지 표시를 판독하는 조사관입니다. "
        f"아래 {len(batch)}개 상품을 각각 독립적으로 판정하세요.\n\n{_JUDGEMENT_RULES}"
    ))]
    for i, (row, images) in enumerate(batch):
        retailer_country = _retailer_country(row["retailer"], row.get("source_site"))
        parts.append(types.Part.from_text(text="\n" + _product_block(i, row, retailer_country, len(images))))
        for img_bytes, mime, _url in images:
            parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))

    response = await client.aio.models.generate_content(
        model=model,
        contents=parts,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=list[OriginBatchItemVerdict],
            temperature=0,
        ),
    )
    verdicts = response.parsed
    if verdicts is None:
        verdicts = [OriginBatchItemVerdict.model_validate(v) for v in json.loads(response.text)]
    return {v.index: _validate_verdict(v) for v in verdicts}


async def _fetch_candidate_products(http: httpx.AsyncClient, run_id: int | None, all_runs: bool) -> list[dict]:
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
            return []
        run_ids = [runs[0]["run_id"]]

    products = []
    seen_urls = set()
    for rid in run_ids:
        resp = await http.get(f"/api/product-sourcing/crawl-runs/{rid}")
        resp.raise_for_status()
        for row in resp.json().get("rows", []):
            url = row.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            products.append(row)
    return products


async def _already_verified_urls(http: httpx.AsyncClient, urls: list[str]) -> set[str]:
    if not urls:
        return set()
    verified: set[str] = set()
    # 백엔드 요청 payload가 너무 커지지 않도록 청크로 나눠 확인
    for i in range(0, len(urls), 500):
        chunk = urls[i:i + 500]
        resp = await http.post("/api/product-origin-verification/check", json={"urls": chunk})
        resp.raise_for_status()
        verified.update(resp.json().get("verified_urls", []))
    return verified


async def _upsert_batch(http: httpx.AsyncClient, batch: list[tuple[dict, list[tuple[bytes, str, str]]]],
                         verdicts_by_index: dict[int, OriginBatchItemVerdict], model: str) -> tuple[int, int]:
    items = []
    missing = 0
    for i, (row, images) in enumerate(batch):
        verdict = verdicts_by_index.get(i)
        if verdict is None:
            missing += 1
            continue
        items.append({
            "url": row["url"],
            "origin_found": verdict.origin_found,
            "origin_text": verdict.origin_text,
            "note": verdict.note,
            "images_used": [u for _, _, u in images],
            "verification_model": model,
        })
    if items:
        resp = await http.post("/api/product-origin-verification/upsert", json={"items": items})
        resp.raise_for_status()
    return len(items), missing


async def main(args):
    from google import genai

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY 환경변수가 필요합니다.")

    backend_url = os.getenv("BACKEND_URL", DEFAULT_BACKEND_URL)

    async with httpx.AsyncClient(base_url=backend_url, timeout=60.0) as http:
        products = await _fetch_candidate_products(http, args.run_id, args.all_runs)
        if not products:
            print("대상 크롤링 회차에서 상품을 찾지 못했습니다.")
            return

        verified = await _already_verified_urls(http, [p["url"] for p in products])
        targets = [p for p in products if p["url"] not in verified]

        print(f"크롤링 후보 상품 {len(products)}건 중 미검증 {len(targets)}건")

        if args.limit:
            targets = targets[: args.limit]

        if args.dry_run:
            for p in targets[:20]:
                print(f"  [dry-run] {p.get('retailer')}/{p.get('source_site')} "
                      f"{p.get('brand')} — {p.get('product_name_en')} (url={p['url'][:80]})")
            if len(targets) > 20:
                print(f"  ... 외 {len(targets) - 20}건")
            return

        # 이미지 다운로드는 별도 httpx 클라이언트(base_url 없음, 절대 URL 그대로 사용)
        async with httpx.AsyncClient(follow_redirects=True) as img_http:
            client = genai.Client(api_key=api_key)

            # 이미지 수집(다운로드)은 배치와 무관하게 상품별로 미리 다 해둔다 — Gemini
            # 호출만 배치로 묶는다.
            rows_with_images: list[tuple[dict, list[tuple[bytes, str, str]]]] = []
            for row in targets:
                try:
                    image_urls = await _collect_images(img_http, row)
                    images = await _download_images(img_http, image_urls)
                except Exception:
                    images = []
                rows_with_images.append((row, images))

            batches = [rows_with_images[i:i + args.batch_size] for i in range(0, len(rows_with_images), args.batch_size)]
            ok, failed = 0, 0
            for bi, batch in enumerate(batches):
                try:
                    verdicts_by_index = await _verify_batch(client, args.model, batch)
                    upserted, missing = await _upsert_batch(http, batch, verdicts_by_index, args.model)
                    ok += upserted
                    failed += missing
                    print(f"  [배치 {bi+1}/{len(batches)}] {upserted}건 판독 완료"
                          + (f", {missing}건 응답 누락(다음 실행에서 재시도)" if missing else ""))
                except Exception as e:
                    failed += len(batch)
                    print(f"  [배치 {bi+1}/{len(batches)} 실패] {type(e).__name__}: {e} — 이 배치 {len(batch)}건은 다음 실행에서 재시도")
                time.sleep(args.sleep)

            print(f"완료: 성공 {ok}건, 실패/누락 {failed}건 (실패한 상품은 캐시에 안 남아 다음 실행에서 재시도됨)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--all-runs", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args))
