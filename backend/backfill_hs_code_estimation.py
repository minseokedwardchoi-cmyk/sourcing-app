"""
backfill_hs_code_estimation.py — reference_data/hs_final_7397.csv(product_sourcing_item
7,397건을 전수 스캔해서 이미 판정해둔 HS코드 결과, HS_CODE_METHODOLOGY.md 참고)를
hs_code_estimation 캐시로 1회 이전한다.

이걸 먼저 돌려야 하는 이유: hs_code_estimate_gemini.py는 "hs_code_estimation에 없는
영어상품명만" Gemini로 새로 판정한다. 이 백필 없이 바로 돌리면 이미 전수 스캔으로
판정해둔 7,397건과 이름이 겹치는 신규 크롤링 상품까지 전부 Gemini로 재판정하게 되어
불필요한 API 비용이 들고, 기존 판정과 다른 값이 나올 위험도 있다.

verify_brands_gemini.py/verify_origin_gemini.py와 동일하게 DB에 직접 붙지 않고 백엔드
HTTP API(GET/POST /api/hs-code-estimation/*)만 호출한다 — 이 프로젝트의 다른 GitHub
Actions 워크플로/자동화 스크립트와 같은 원칙(CI 러너가 DATABASE_URL로 Postgres에
직접 붙지 않음).

캐시 키는 영어상품명 정규화값(name_key) — 같은 상품명이 CSV에 여러 번 나오면(유통사별
중복) hs_code가 있는 행을 우선 채택한다.

hs_code가 비어있는 행(flagged_non_food_mismatch 등 무관상품 판정)도 그대로 캐시에
넣는다 — "hs_code_estimation에 없음"과 "판정했는데 무관상품이라 비워둠"을 구분해야
Gemini가 같은 상품을 또 판정하지 않는다.

실행:
  BACKEND_URL=https://sourcing-backend-ucp5.onrender.com python backfill_hs_code_estimation.py
  --dry-run 으로 몇 건이 대상인지만 먼저 확인 가능.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os

import httpx

from product_name_key_normalize import normalize_product_name_key

CSV_PATH = "reference_data/hs_final_7397.csv"
DEFAULT_BACKEND_URL = "https://sourcing-backend-ucp5.onrender.com"
UPSERT_BATCH = 200

# CSV의 status 값 중 사람이 아직 확인 안 한 건 신뢰도가 없으니 캐시에 안 넣는다 —
# 캐시에 없으면 hs_code_estimate_gemini.py가 나중에 새로 판정하게 둔다(재판정이
# "확인 안 된 값을 그대로 굳히는 것"보다 낫다).
_SKIP_STATUS = {"needs_manual_review"}


def _load_target_rows() -> dict[str, dict]:
    with open(CSV_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    print(f"{CSV_PATH}: {len(rows)}행 로드")

    by_key: dict[str, dict] = {}
    skipped_no_name = 0
    skipped_status = 0
    skipped_dup = 0
    for row in rows:
        name_en = (row.get("영어상품명") or "").strip()
        if not name_en:
            skipped_no_name += 1
            continue
        status = (row.get("status") or "").strip()
        if status in _SKIP_STATUS:
            skipped_status += 1
            continue
        key = normalize_product_name_key(name_en)
        if not key:
            skipped_no_name += 1
            continue
        if key in by_key:
            skipped_dup += 1
            if by_key[key].get("hs_code") or not row.get("hs_code"):
                continue
        by_key[key] = row

    print(f"고유 name_key {len(by_key)}개로 묶임 "
          f"(상품명 없음 {skipped_no_name}건, needs_manual_review 제외 {skipped_status}건, "
          f"중복 name_key {skipped_dup}건)")
    return by_key


async def main(dry_run: bool, limit: int | None):
    by_key = _load_target_rows()
    backend_url = os.getenv("BACKEND_URL", DEFAULT_BACKEND_URL).rstrip("/")

    async with httpx.AsyncClient(base_url=backend_url, timeout=30) as http:
        resp = await http.get("/api/hs-code-estimation/keys")
        resp.raise_for_status()
        existing_keys = set(resp.json().get("name_keys", []))

        target_keys = [k for k in by_key if k not in existing_keys]
        print(f"이미 캐시에 있는 상품 {len(existing_keys & set(by_key))}개는 스킵, "
              f"신규 백필 대상 {len(target_keys)}개")

        if limit:
            target_keys = target_keys[:limit]

        if dry_run:
            for k in target_keys[:30]:
                row = by_key[k]
                print(f"  [dry-run] {k[:80]!r}  hs_code={row.get('hs_code')!r} status={row.get('status')!r}")
            if len(target_keys) > 30:
                print(f"  ... 외 {len(target_keys) - 30}개")
            return

        inserted = 0
        for i in range(0, len(target_keys), UPSERT_BATCH):
            chunk = target_keys[i:i + UPSERT_BATCH]
            items = []
            for key in chunk:
                row = by_key[key]
                items.append({
                    "name_key": key,
                    "product_name_en": (row.get("영어상품명") or "").strip() or None,
                    "product_type_hint": (row.get("유형") or "").strip() or None,
                    "hs_code": (row.get("hs_code") or "").strip() or None,
                    "confidence": (row.get("confidence") or "").strip() or None,
                    "reason": (row.get("reason") or "").strip() or None,
                    "evidence_url": (row.get("evidence_url") or "").strip() or None,
                    "status": (row.get("status") or "").strip() or None,
                    "estimation_source": "seed_hs_final_7397",
                    "estimation_model": None,
                })
            resp = await http.post("/api/hs-code-estimation/upsert", json={"items": items})
            resp.raise_for_status()
            inserted += resp.json().get("upserted", 0)
            print(f"  {min(i + UPSERT_BATCH, len(target_keys))}/{len(target_keys)} 업로드")

        print(f"완료: {inserted}건 upsert")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, limit=args.limit))
