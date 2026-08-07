"""
shrink_product_sourcing_images.py — 이미 백필된 product_sourcing_item.image_data를
더 작은 썸네일로 다시 리사이즈해서 덮어쓴다 (외부 URL 재요청 없이 DB에 있는
바이트만 다시 압축 — 순수 로컬 처리라 빠름).

배경: 원본형식 다운로드 엔드포인트가 ~7,200개 이미지를 워크북에 올릴 때
Render 백엔드 인스턴스 메모리가 부족해 죽는 걸(502) 실제로 겪었다. 텍스트
골격과 이미지 주입을 분리해서 피크 메모리를 낮췄지만(product_sourcing_exporter.py
참고), openpyxl은 저장 시점에 어차피 전체 이미지 바이트를 한꺼번에 들고
있어야 해서 총 이미지 용량 자체를 더 줄여 안전마진을 확보한다.

실행 (프로덕션 DB 대상):
  DATABASE_URL="postgresql+asyncpg://user:pass@host/db" python shrink_product_sourcing_images.py
"""
from __future__ import annotations
import argparse
import asyncio
from io import BytesIO

from PIL import Image as PILImage
from sqlalchemy import text

from database import AsyncSessionLocal

MAX_DIM = 130
JPEG_QUALITY = 75


def _shrink(raw: bytes) -> bytes | None:
    try:
        img = PILImage.open(BytesIO(raw))
        img.load()
    except Exception:
        return None
    if max(img.size) <= MAX_DIM:
        return raw  # 이미 충분히 작으면 그대로 둠
    img = img.convert("RGB")
    img.thumbnail((MAX_DIM, MAX_DIM))
    out = BytesIO()
    img.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


async def run(batch_size: int, dry_run: bool) -> None:
    async with AsyncSessionLocal() as session:
        ids = [r[0] for r in (await session.execute(
            text("SELECT id FROM product_sourcing_item WHERE image_data IS NOT NULL ORDER BY id")
        )).all()]

    print(f"대상 {len(ids)}건")
    if dry_run or not ids:
        return

    total_before = total_after = 0
    changed = 0
    for i in range(0, len(ids), batch_size):
        batch_ids = ids[i:i + batch_size]
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                text("SELECT id, image_data FROM product_sourcing_item WHERE id = ANY(:ids)"),
                {"ids": batch_ids},
            )).mappings().all()

            for row in rows:
                raw = bytes(row["image_data"])
                total_before += len(raw)
                shrunk = _shrink(raw)
                if shrunk is None:
                    total_after += len(raw)
                    continue
                total_after += len(shrunk)
                if shrunk is raw:
                    continue
                changed += 1
                await session.execute(
                    text("UPDATE product_sourcing_item SET image_data = :data, image_mime = 'image/jpeg' WHERE id = :id"),
                    {"data": shrunk, "id": row["id"]},
                )
            await session.commit()

        print(f"  {min(i + batch_size, len(ids))}/{len(ids)} 처리 (변경 {changed})")

    print(f"완료: {changed}건 축소. 총 용량 {total_before/1024/1024:.1f}MB -> {total_after/1024/1024:.1f}MB")


def main() -> None:
    parser = argparse.ArgumentParser(description="product_sourcing_item.image_data 재압축")
    parser.add_argument("--batch-size", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.batch_size, args.dry_run))


if __name__ == "__main__":
    main()
