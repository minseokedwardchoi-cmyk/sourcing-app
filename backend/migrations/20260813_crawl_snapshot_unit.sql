-- 크롤링 스냅샷(product_sourcing_crawl_snapshot_item)에 단량("unit") 컬럼 추가.
-- product_sourcing_item과 달리 크롤러가 원문 상품명만 가져오고 사람이 단량을 따로
-- 입력해주지 않으므로, 저장 시점에 product_name_en에서 unit_converter.extract_unit_from_product_name()으로
-- 직접 뽑아 채운다 (main.py의 upload_product_sourcing_crawl_snapshot 참고).
ALTER TABLE product_sourcing_crawl_snapshot_item
    ADD COLUMN IF NOT EXISTS unit VARCHAR(50);
