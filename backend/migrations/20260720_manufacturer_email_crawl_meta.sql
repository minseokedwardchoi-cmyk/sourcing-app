-- 제조사 홈페이지 크롤링으로 채워진 회사 대표 이메일(정보/영업/문의 등)을
-- 수기 입력 값과 구분하고, 크롤링 재시도 주기를 관리하기 위한 메타 컬럼.
ALTER TABLE import_history
    ADD COLUMN IF NOT EXISTS email_source     VARCHAR(20),
    ADD COLUMN IF NOT EXISTS email_crawled_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS ix_email_crawled_at ON import_history (email_crawled_at);
