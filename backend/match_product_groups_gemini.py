"""
match_product_groups_gemini.py — 유통사 간 동일 제품 매칭(brand_group_key/
product_group_key)을 Gemini API로 자동화한다.

기존에는 Claude Code 세션이 matching_prompt.md 절차를 사람처럼(전수 스캔 →
브랜드 언어 표기 차이 인식 → 포맷/라인 구분 → 번들 판별) 수행해서 83개 품목
유형·7,397행 전체를 채웠다(2026-08-12 기준 완료). 이 스크립트는 그 판정
기준을 그대로 Gemini API 자동화로 옮긴 것 — verify_brands_gemini.py와 같은
패턴이다.

**언제 필요한가**: product_sourcing_importer.import_product_sourcing()은 엑셀
재업로드 시 (품목유형, 유통사, 브랜드한글명, 영문상품명, 단량)이 이전과 동일한
행에는 기존 그룹키를 그대로 이어붙이지만, 완전히 새로 등장한 상품(순위 밖으로
밀려났던 게 새로 진입, 신규 브랜드 등)은 brand_group_key가 NULL로 남는다. 이
스크립트는 그 NULL 행이 있는 품목유형만 골라 전수 재검토하고, 부족한 행만
채운다(이미 채워진 행은 컨텍스트로만 보여주고 덮어쓰지 않는다).

DB에 직접 붙지 않고 백엔드 HTTP API(GET /api/product-sourcing/group-keys/*,
POST .../upsert)만 호출한다 — 이 프로젝트의 다른 GitHub Actions 워크플로와
동일하게, CI 러너가 DATABASE_URL로 Postgres에 직접 붙지 않는 원칙을 따른다.

실행:
  GEMINI_API_KEY=... BACKEND_URL=https://sourcing-backend-ucp5.onrender.com \\
    python match_product_groups_gemini.py --limit=5

옵션:
  --limit=N       이번 실행에서 처리할 품목유형(product_type) 수 상한 (기본 5, API 비용 제어)
  --sleep=SEC     품목유형 사이 호출 간격 (기본 2초, 레이트리밋 방지)
  --model=NAME    Gemini 모델명 (기본 gemini-3.6-flash)
  --dry-run       대상 품목유형만 출력하고 API 호출/DB 쓰기 안 함
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time

import httpx
from pydantic import BaseModel, Field

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
DEFAULT_BACKEND_URL = "https://sourcing-backend-ucp5.onrender.com"

_SLUG_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _slugify(raw: str) -> str:
    """모델 출력을 '영문 소문자 + 언더스코어' 규칙으로 보정한다 (matching_prompt.md 키 작성 규칙)."""
    s = raw.strip().lower()
    s = _SLUG_PUNCT_RE.sub("_", s)
    return s.strip("_")


_MATCHING_PRINCIPLES = """
매칭 원칙 (반드시 지킬 것):
1. 전수 스캔 필수 — 아래 목록의 모든 행에 대해 빠짐없이 assignments 하나씩 출력한다.
2. 브랜드 인식은 언어 표기 차이까지 커버한다 (예: "Bertolli" = "ベルトーリ" = "베르톨리").
   브랜드명 문자열이 다르다고 바로 다른 브랜드로 취급하지 말고, 실제로 같은 브랜드인지 판단한다.
3. 포맷(병/스프레이/스퀴즈/캔 등 용기 형태) 차이는 매칭을 막는 이유가 아니다. 상품명에
   같은 라인명(맛/등급)이 명시돼 있으면 같은 제품으로 묶는다. 라인명이 아예 안 적혀
   있어 판단이 안 서면 억지로 묶지 말고 별도 제품으로 둔다.
4. 이름이 비슷하다고 자동으로 같은 제품으로 보지 않는다 (예: "Smooth"와 "Extra Smooth"는
   실제로 다른 라인일 수 있다). 확신이 없으면 보수적으로 분리한다.
5. "Organic"/"유기농" 여부, 맛/품종, 등급이 다르면 다른 제품이다 — 브랜드가 같아도 별도
   product_group_key로 분리한다.
6. 번들/멀티팩(서로 다른 두 제품을 묶은 상품)은 어느 한쪽에 억지로 합치지 말고, 별도의
   product_group_key(예: "<브랜드>_bundle_<설명>")를 부여한다.

키 작성 규칙:
- brand_group_key: 정규화된 브랜드명, 영문 소문자 + 언더스코어 (예: bertolli, california_olive_ranch).
  "(NB)" 같은 접미사, 괄호, 공백은 제거한다.
- product_group_key: brand_group_key 뒤에 라인/등급을 붙인 형태 (예: bertolli_evoo_regular,
  bertolli_evoo_organic). 라인 구분이 없는 단일 제품 브랜드는 brand_group_key와 동일하게 써도 된다.
- 이미 그룹핑된 행(brand_group_key/product_group_key가 채워져 표시된 행)과 같은 브랜드/제품이면
  반드시 그 값을 그대로 재사용한다 (새로 만들지 않는다).
- 아래 "다른 품목유형에서 이미 쓰인 브랜드 키" 목록에 해당 브랜드가 있으면 그 값을 그대로
  재사용한다 (같은 브랜드가 품목유형마다 다른 키로 갈라지지 않도록).
""".strip()


class GroupKeyAssignment(BaseModel):
    id: int = Field(description="대상 행의 id (입력 목록에 있던 값 그대로)")
    brand_group_key: str = Field(description="정규화된 브랜드 키")
    product_group_key: str = Field(description="브랜드 키 + 라인/등급 구분")


class GroupKeyAssignments(BaseModel):
    assignments: list[GroupKeyAssignment]


def _format_rows(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        existing = ""
        if r.get("brand_group_key"):
            existing = f"  [기존 그룹: {r['brand_group_key']} / {r['product_group_key']}]"
        lines.append(
            f"id={r['id']} | {r['retailer']} #{r['rank']} | 브랜드(한): {r.get('brand_kr') or '-'} | "
            f"브랜드(영): {r.get('brand_en') or '-'} | 상품명: {r.get('product_name_en') or '-'} | "
            f"단량: {r.get('unit') or '-'}{existing}"
        )
    return "\n".join(lines)


def _build_prompt(product_type: str, rows: list[dict], known_brand_keys: list[str]) -> str:
    vocab_txt = ", ".join(known_brand_keys[:500]) if known_brand_keys else "(없음)"
    pending_ids = [r["id"] for r in rows if not r.get("brand_group_key")]
    return f"""당신은 여러 유통사(아마존/월마트/샘스클럽/이온몰)의 인기상품 리스트에서,
같은 품목유형 안에 흩어진 "같은 브랜드/같은 제품(용량·유통사만 다름)" 행을 찾아
브랜드 그룹핑 키를 부여하는 작업을 합니다.

품목유형: {product_type}

{_MATCHING_PRINCIPLES}

다른 품목유형에서 이미 쓰인 브랜드 키 (해당하면 재사용):
{vocab_txt}

이 품목유형의 전체 행 목록 (전수 검토 대상 — "[기존 그룹]"이 붙은 행은 이미
확정된 값이니 그대로 유지하고, 새 행을 같은 브랜드/제품으로 판단했다면 그
값을 그대로 쓰세요):
{_format_rows(rows)}

출력은 assignments 배열 하나로, id={pending_ids} (기존 그룹이 없는 행) 각각에
대해 brand_group_key/product_group_key를 정확히 하나씩 채우세요. 이미 그룹핑된
행은 출력하지 않아도 됩니다."""


async def _fetch_pending_types(http: httpx.AsyncClient) -> list[str]:
    resp = await http.get("/api/product-sourcing/group-keys/pending-types")
    resp.raise_for_status()
    return resp.json().get("product_types", [])


async def _fetch_rows(http: httpx.AsyncClient, product_type: str) -> list[dict]:
    resp = await http.get("/api/product-sourcing/group-keys/rows", params={"product_type": product_type})
    resp.raise_for_status()
    return resp.json().get("rows", [])


async def _fetch_vocab(http: httpx.AsyncClient) -> list[str]:
    resp = await http.get("/api/product-sourcing/group-keys/vocab")
    resp.raise_for_status()
    return resp.json().get("brand_group_keys", [])


async def _upsert(http: httpx.AsyncClient, items: list[dict]) -> int:
    resp = await http.post("/api/product-sourcing/group-keys/upsert", json={"items": items})
    resp.raise_for_status()
    return resp.json().get("updated", 0)


async def _match_one_type(client, model: str, product_type: str, rows: list[dict],
                           known_brand_keys: list[str]) -> list[dict]:
    from google.genai import types

    prompt = _build_prompt(product_type, rows, known_brand_keys)
    resp = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GroupKeyAssignments,
            temperature=0.1,
        ),
    )
    parsed = resp.parsed
    if parsed is None:
        parsed = GroupKeyAssignments.model_validate(json.loads(resp.text))

    valid_ids = {r["id"] for r in rows if not r.get("brand_group_key")}
    items = []
    for a in parsed.assignments:
        if a.id not in valid_ids:
            continue
        items.append({
            "id": a.id,
            "brand_group_key": _slugify(a.brand_group_key),
            "product_group_key": _slugify(a.product_group_key),
        })
    return items


async def main(args):
    from google import genai

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY 환경변수가 필요합니다.")

    backend_url = os.getenv("BACKEND_URL", DEFAULT_BACKEND_URL)

    async with httpx.AsyncClient(base_url=backend_url, timeout=60.0) as http:
        pending_types = await _fetch_pending_types(http)
        if not pending_types:
            print("미매칭 품목유형 없음 — 종료")
            return

        print(f"미매칭 품목유형 {len(pending_types)}개 중 이번 실행 대상 {min(args.limit, len(pending_types))}개")
        targets = pending_types[: args.limit]

        if args.dry_run:
            for pt in targets:
                print(f"  [dry-run] {pt}")
            return

        known_brand_keys = await _fetch_vocab(http)
        client = genai.Client(api_key=api_key)

        ok, failed = 0, 0
        for pt in targets:
            try:
                rows = await _fetch_rows(http, pt)
                items = await _match_one_type(client, args.model, pt, rows, known_brand_keys)
                updated = await _upsert(http, items) if items else 0
                # 새로 쓴 브랜드 키를 다음 품목유형 처리 시 재사용 어휘에 반영
                known_brand_keys = list({*known_brand_keys, *[i["brand_group_key"] for i in items]})
                ok += 1
                print(f"  [OK] {pt}: {len(rows)}행 중 {len(items)}행 매칭 → {updated}행 갱신")
            except Exception as e:
                failed += 1
                print(f"  [FAIL] {pt}: {type(e).__name__}: {e}")
            time.sleep(args.sleep)

        print(f"완료: 성공 {ok}개, 실패 {failed}개 (실패한 품목유형은 다음 실행에서 재시도됨)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args))
