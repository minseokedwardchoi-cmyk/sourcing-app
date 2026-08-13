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
    python verify_origin_gemini.py --limit=20

옵션은 verify_brands_gemini.py와 동일한 구조 (--run-id/--all-runs/--limit/--sleep/
--model/--dry-run).
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

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_BACKEND_URL = "https://sourcing-backend-ucp5.onrender.com"

MAX_IMAGES = 4
IMAGE_FETCH_TIMEOUT = 12.0
JINA_FETCH_TIMEOUT = 20.0

_VALID_ORIGIN_FOUND = {"Y", "E", "N"}

_IMG_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_SKIP_IMAGE_HINTS = ("sprite", "icon", "logo", "1x1", "pixel", "spinner", "loading", "blank.gif")


class OriginVerdict(BaseModel):
    origin_found: str = Field(description="사진에서 실제 문구를 찾았으면 'Y', 문구는 없지만 추정 가능하면 'E', 근거가 아예 없으면 'N'")
    origin_text: str = Field(description="Y면 라벨 문구 그대로(또는 요약), E면 '국가명(추정)' 형식, N이면 빈 문자열")
    note: str = Field(description="판정 근거·특이사항 (한국어). E일 때는 추정 근거를 반드시 남길 것")


def _judgement_rules(retailer_country: str, retailer: str) -> str:
    return f"""
판정 기준 (반드시 순서대로 적용):
1. 제공된 사진에서 "Product of ~", "Made in ~", "Manufactured in ~", "Packed in ~",
   "Imported from ~" 같은 원산지 표시 문구를 먼저 찾는다. 영양정보/성분표만 보이는
   사진은 무시하고 포장 전체가 보이는 사진을 우선한다. 여러 나라가 같이 언급되면
   ("Product of Italy, Spain, Greece" 등) 있는 그대로 기록하고 억지로 한 나라로
   단정하지 않는다. 문구를 실제로 찾았으면 origin_found="Y", origin_text에 그
   문구(또는 짧은 요약)를 적는다. 일본 상품이면 "国内製造"(국내제조) 표기도 실측
   문구로 인정한다.
2. 문구를 못 찾았거나(사진에 없음), 사진 자체가 아예 없으면 아래 추정 규칙을 적용해서
   origin_found="E"로 표시하고 origin_text에 "국가명(추정)" 형식으로 적는다:
   - 이 상품은 {retailer_country} 소매채널({retailer})에서 판매된다.
   - {retailer_country}가 미국이면: 미국 원산지표시법(COOL)상 가공식품(마요네즈·
     크래커·시리얼처럼 원재료를 가공한 식품)은 원산지 표시 의무 자체가 없다.
     브랜드가 미국 내수 브랜드/가공식품으로 보이고 딱히 수입 근거가 없으면
     "미국(추정)"으로 적고 note에 "가공식품 COOL 적용제외 - 미국 내수 브랜드로
     추정"이라고 남긴다. 다른 근거(브랜드 원산지가 널리 알려진 경우 등)로 실제
     원산지가 짐작되면 그 나라 + "(추정)" + note에 근거.
   - {retailer_country}가 일본이면: 일본은 가공식품도 원산지 표시가 원칙적으로
     필요해서 미국식 예외가 성립하지 않는다. 이 브랜드가 일본 내수 제조업체의
     자체상품이고, 이 상품(원료)을 일본이 상업적으로 거의 생산하지 않는다는 게
     일반 상식이면(기후·지리상 재배/생산이 없는 작물·원료), 정확한 원산지국은
     불명이지만 원료를 해외에서 수입해 일본에서 가공했을 가능성이 높다는 뜻으로
     "다국적 블렌드(추정)"라고 적고 note에 그 판단 근거(어떤 원료가 일본에서
     생산 안 되는지)를 남긴다.
   - {retailer_country}가 말레이시아면: 이 브랜드가 여러 나라 유통사에서도 팔리는
     글로벌 브랜드로 보이면, 그 브랜드에 일반적으로 알려진 원산지를 근거로 추정한다
     (note에 "글로벌 브랜드로 알려진 원산지 기준 추정"이라고 남길 것). 말레이시아
     로컬 브랜드면 일본과 같은 논리(말레이시아가 이 원료를 상업 생산하지 않으면
     수입 추정)를 적용한다.
3. 억지로 추정하지 말 것 — 위 규칙으로도 근거가 없으면 origin_found="N"(확인불가)
   으로 정직하게 남기고 origin_text는 빈 문자열로 둔다. 틀린 추정보다 "모른다"가 낫다.
4. "확인필요"라는 애매한 값은 절대 쓰지 않는다 — Y/E/N 중 하나만 쓴다.
5. 사진이 하나도 제공되지 않았다면 1번은 건너뛰고 바로 2번(추정) 또는 3번(확인불가)을
   적용한다 — 이 경우 note에 "사진 없음, 텍스트 정보만으로 판단"이라고 남길 것.
""".strip()


def _retailer_country(retailer: str, source_site: str | None) -> str:
    if retailer == "aeon":
        if source_site == "aeon-my":
            return "말레이시아"
        return "일본"
    return "미국"  # walmart / samsclub / amazon


def _product_prompt(row: dict, retailer_country: str) -> str:
    rules = _judgement_rules(retailer_country, row["retailer"])
    return f"""당신은 식품 패키지 사진에서 원산지 표시를 판독하는 조사관입니다.

상품유형: {row.get('product_type') or '(정보없음)'}
브랜드: {row.get('brand') or '(정보없음)'}
상품명(원어): {row.get('product_name_en') or '(정보없음)'}
유통사: {row['retailer']} ({retailer_country})

{rules}"""


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
        if len(urls) >= MAX_IMAGES:
            break
    return urls


async def _collect_images(http: httpx.AsyncClient, row: dict) -> list[str]:
    if row.get("image_urls"):
        return list(row["image_urls"])[:MAX_IMAGES]

    urls = []
    if row.get("image_url"):
        urls.append(row["image_url"])

    if row["retailer"] in ("amazon", "aeon") and row.get("url"):
        extra = await _fetch_extra_images_via_jina(http, row["url"])
        for u in extra:
            if u not in urls:
                urls.append(u)
            if len(urls) >= MAX_IMAGES:
                break

    return urls[:MAX_IMAGES]


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


def _validate_verdict(v: OriginVerdict) -> OriginVerdict:
    if v.origin_found not in _VALID_ORIGIN_FOUND:
        v.origin_found = "N"
        v.origin_text = ""
    if v.origin_found == "N":
        v.origin_text = ""
    return v


async def _verify_one_product(client, model: str, row: dict, images: list[tuple[bytes, str, str]]):
    from google.genai import types

    retailer_country = _retailer_country(row["retailer"], row.get("source_site"))
    prompt = _product_prompt(row, retailer_country)

    parts = [types.Part.from_text(text=prompt)]
    for img_bytes, mime, _url in images:
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))

    response = await client.aio.models.generate_content(
        model=model,
        contents=parts,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=OriginVerdict,
            temperature=0,
        ),
    )
    verdict = response.parsed
    if verdict is None:
        verdict = OriginVerdict.model_validate(json.loads(response.text))
    return _validate_verdict(verdict)


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


async def _upsert(http: httpx.AsyncClient, url: str, verdict: OriginVerdict,
                   images_used: list[str], model: str):
    resp = await http.post("/api/product-origin-verification/upsert", json={
        "items": [{
            "url": url,
            "origin_found": verdict.origin_found,
            "origin_text": verdict.origin_text,
            "note": verdict.note,
            "images_used": images_used,
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

            ok, failed = 0, 0
            for row in targets:
                try:
                    image_urls = await _collect_images(img_http, row)
                    images = await _download_images(img_http, image_urls)
                    verdict = await _verify_one_product(client, args.model, row, images)
                    await _upsert(http, row["url"], verdict, [u for _, _, u in images], args.model)
                    ok += 1
                    print(f"  [OK] {row.get('brand')} / {row.get('product_name_en')} "
                          f"→ {verdict.origin_found} {verdict.origin_text} (사진 {len(images)}장)")
                except Exception as e:
                    failed += 1
                    print(f"  [FAIL] {row.get('url')}: {type(e).__name__}: {e}")
                time.sleep(args.sleep)

            print(f"완료: 성공 {ok}건, 실패 {failed}건 (실패한 상품은 캐시에 안 남아 다음 실행에서 재시도됨)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--all-runs", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args))
