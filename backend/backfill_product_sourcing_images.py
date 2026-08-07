"""
backfill_product_sourcing_images.py — image_url만 있고 image_data가 없는
product_sourcing_item 행에 대해 실제 이미지를 받아와 리사이즈 후 DB에 저장.

원본 파일에는 모든 순위 칸에 사진이 삽입돼 있었지만, import_product_sourcing은
raw 시트에 실제 호스팅 URL이 있는 유통사(월마트/샘스클럽)는 URL만 저장하고
그림 바이트는 버렸다(중복 저장 방지). '원본 형식 다운로드' 기능에서 매번
~7,000개 URL을 실시간으로 새로 받아오면 느리고 죽은 링크에 취약하므로,
1회 백필로 전부 DB에 영구 저장해둔다.

이미지는 최대 220px로 축소 + JPEG 재인코딩 후 저장한다 — 카드 레이아웃에서
어차피 작게(썸네일 크기) 표시되고, DB 용량/내보내기 시 메모리 사용량도
원본 그대로 저장하는 것보다 훨씬 작다.

실행 (프로덕션 DB 대상 — DATABASE_URL을 직접 넘겨서 실행):
  DATABASE_URL="postgresql+asyncpg://user:pass@host/db" python backfill_product_sourcing_images.py
  --dry-run 으로 대상 건수만 먼저 확인 가능. --limit으로 소량 테스트 가능.
"""
from __future__ import annotations
import argparse
import asyncio
from io import BytesIO

import httpx
from PIL import Image as PILImage
from sqlalchemy import text

from database import AsyncSessionLocal

MAX_DIM = 220
JPEG_QUALITY = 82
REQUEST_TIMEOUT = 12.0


def _resize_to_jpeg(raw: bytes) -> bytes | None:
    try:
        img = PILImage.open(BytesIO(raw))
        img.load()
    except Exception:
        return None

    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = PILImage.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    img.thumbnail((MAX_DIM, MAX_DIM))
    out = BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


async def _fetch_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, row) -> tuple[int, bytes | None]:
    async with sem:
        try:
            resp = await client.get(row["image_url"], timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = _resize_to_jpeg(resp.content)
            return row["id"], data
        except Exception:
            return row["id"], None


async def run(limit: int | None, batch_size: int, concurrency: int, dry_run: bool) -> None:
    async with AsyncSessionLocal() as session:
        query = (
            "SELECT id, image_url FROM product_sourcing_item "
            "WHERE image_data IS NULL AND image_url IS NOT NULL ORDER BY id"
        )
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = (await session.execute(text(query))).mappings().all()

    print(f"대상 {len(rows)}건 (배치 {batch_size} / 동시요청 {concurrency})")
    if dry_run or not rows:
        return

    sem = asyncio.Semaphore(concurrency)
    ok = fail = 0
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            results = await asyncio.gather(*[_fetch_one(client, sem, r) for r in batch])

            async with AsyncSessionLocal() as session:
                for row_id, data in results:
                    if data is None:
                        fail += 1
                        continue
                    ok += 1
                    await session.execute(
                        text("UPDATE product_sourcing_item SET image_data = :data, image_mime = 'image/jpeg' WHERE id = :id"),
                        {"data": data, "id": row_id},
                    )
                await session.commit()

            print(f"  {min(i + batch_size, len(rows))}/{len(rows)} 처리 (성공 {ok} / 실패 {fail})")

    print(f"완료: 성공 {ok}건, 실패 {fail}건")


def main() -> None:
    parser = argparse.ArgumentParser(description="product_sourcing_item image_url → image_data 백필")
    parser.add_argument("--limit", type=int, default=None, help="처리할 최대 행 수 (테스트용)")
    parser.add_argument("--batch-size", type=int, default=200, help="DB 커밋 배치 크기")
    parser.add_argument("--concurrency", type=int, default=10, help="동시 다운로드 수")
    parser.add_argument("--dry-run", action="store_true", help="대상 건수만 출력하고 종료")
    args = parser.parse_args()
    asyncio.run(run(args.limit, args.batch_size, args.concurrency, args.dry_run))


if __name__ == "__main__":
    main()
