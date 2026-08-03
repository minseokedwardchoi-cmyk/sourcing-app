"""
models.py — DB 테이블 정의
테이블 구조는 Excel 컬럼 기반으로 설계.
실제 Excel 컬럼명이 달라질 경우 FIELD_MAP(importer.py)만 수정하면 됨.
"""
from sqlalchemy import (
    Column, Integer, String, Date, DateTime, Text, Index, UniqueConstraint, Numeric
)
from database import Base


class ImportHistory(Base):
    """
    수입/OEM 이력 원본 테이블 (raw records).
    Excel 1행 = DB 1행.
    """
    __tablename__ = "import_history"

    id               = Column(Integer, primary_key=True, autoincrement=True)

    # ── 상품 정보 ──────────────────────────────────────────
    category         = Column(String(100),  nullable=True,  comment="구분 (PB/NB/부자재 등)")
    mc               = Column(String(100),  nullable=True,  comment="MC (상품 카테고리: 과자/제과, 음료/커피 등)")
    sku_name         = Column(String(500),  nullable=False, comment="SKU명")
    sku_name_en      = Column(String(500),  nullable=True,  comment="SKU 영문명 (내부 매칭용, 프론트 미노출)")
    import_type      = Column(String(50),   nullable=True,  comment="OEM/수입 여부")

    # ── 수입업체 ───────────────────────────────────────────
    importer         = Column(String(300),  nullable=True,  comment="수입업체명")

    # ── 제조사 정보 ────────────────────────────────────────
    manufacturer     = Column(String(300),  nullable=True,  comment="제조사명")
    factory          = Column(String(300),  nullable=True,  comment="해외제조업소")
    country          = Column(String(100),  nullable=True,  comment="제조국")
    location         = Column(String(300),  nullable=True,  comment="소재지")

    # ── 연락처 ────────────────────────────────────────────
    email            = Column(String(300),  nullable=True,  comment="이메일 (복수 시 콤마 구분)")
    homepage         = Column(String(500),  nullable=True,  comment="홈페이지 URL")
    email_source     = Column(String(20),   nullable=True,  comment="이메일 출처 (manual/crawled)")
    email_crawled_at = Column(DateTime,     nullable=True,  comment="홈페이지 이메일 크롤링 마지막 시도 시각")

    # ── 날짜 ──────────────────────────────────────────────
    import_date      = Column(Date,         nullable=True,  comment="수입일자")
    process_date     = Column(Date,         nullable=True,  comment="처리일자")

    # ── OEM / 소싱 ────────────────────────────────────────
    oem_status       = Column(String(100),  nullable=True,  comment="OEM 가능성")
    oem_memo         = Column(Text,         nullable=True,  comment="OEM 메모")
    manager_mc       = Column(String(100),  nullable=True,  comment="담당 MC")

    # ── 상품 분류 ─────────────────────────────────────────
    product_type     = Column(String(200),  nullable=True,  comment="상품유형")
    product_category = Column(String(500),  nullable=True,  comment="취급 카테고리 (콤마 구분)")
    certificates     = Column(Text,         nullable=True,  comment="인증서 (콤마 구분)")

    # ── MD 컨택 관리 ──────────────────────────────────────────
    contact_status   = Column(String(100),  nullable=True,  comment="MD 컨택 상태 (컨택이력 없음/컨택 중/거래성사 등)")
    md_name          = Column(String(100),  nullable=True,  comment="담당 MD명")

    __table_args__ = (
        # 집계 기준 복합 인덱스 (수입횟수 카운팅용)
        Index("ix_agg_key", "category", "mc", "sku_name", "import_type",
              "importer", "manufacturer", "country"),
        # 개별 컬럼 인덱스
        Index("ix_sku_name",     "sku_name"),
        Index("ix_manufacturer", "manufacturer"),
        Index("ix_importer",     "importer"),
        Index("ix_mc",           "mc"),
        Index("ix_country",      "country"),
        Index("ix_import_date",  "import_date"),
    )


class CountryImportStat(Base):
    """
    국가별 대한민국 수입금액 통계 (정적 참고자료, 관세청 통계 등에서 수동 입력).
    """
    __tablename__ = "country_import_stat"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    country            = Column(String(100), nullable=False, unique=True, comment="국가명")
    total_amount_usd_k = Column(Numeric,      nullable=False, comment="수입금액 (천달러)")

    __table_args__ = (
        Index("ix_cis_country", "country"),
    )


class CountryTopItem(Base):
    """
    국가별 주요 수입품목 TOP 10 (정적 참고자료).
    """
    __tablename__ = "country_top_item"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    country   = Column(String(100), nullable=False, comment="국가명")
    rank      = Column(Integer,     nullable=False, comment="순위 (1~10)")
    item_name = Column(String(200), nullable=False, comment="수입품목명")
    pct       = Column(Numeric,     nullable=False, comment="비중 (%)")

    __table_args__ = (
        UniqueConstraint("country", "rank", name="uq_cti_country_rank"),
        Index("ix_cti_country", "country"),
    )


class ProductSourcingItem(Base):
    """
    품목별(유형) × 유통사(월마트/아마존/샘스클럽/이온몰) 순위 상품 + 소싱 리스크 정보.
    ESI Top40 리서치 엑셀(유형별카드 시트 + 상품리스트(raw) 시트를 유형·유통사·순위로
    조인)에서 적재. product_sourcing_importer.py 참고.
    """
    __tablename__ = "product_sourcing_item"

    id                  = Column(Integer, primary_key=True, autoincrement=True)

    product_type        = Column(String(300), nullable=False, comment="품목 유형 (예: OLITALIA 엑스트라버진 올리브유)")
    retailer             = Column(String(20),  nullable=False, comment="유통사 (walmart/amazon/samsclub/aeon)")
    retailer_label       = Column(String(50),  nullable=True,  comment="유통사 한글명 (월마트/아마존/샘스클럽/이온몰)")
    ranking_method       = Column(String(100), nullable=True,  comment="순위 산출 기준 (best_seller/top_selling 등)")
    sample_note          = Column(String(200), nullable=True,  comment="원본 순위 설명 (예: 상위 40개 · PB 제외)")
    rank                 = Column(Integer,     nullable=False, comment="유통사 내 순위")

    brand_kr             = Column(String(200), nullable=True,  comment="브랜드 한국명")
    brand_en             = Column(String(200), nullable=True,  comment="브랜드 영문명")
    product_name_en      = Column(String(500), nullable=True,  comment="영문 상품명")

    price_usd            = Column(Numeric,     nullable=True,  comment="가격 (USD)")
    origin                = Column(Text,        nullable=True,  comment="원산지")
    unit                 = Column(String(100), nullable=True,  comment="단량/용량")

    key_criteria_label   = Column(String(100), nullable=True,  comment="핵심기준 항목명 (품목별로 다름: 산도/난황 함량 등)")
    key_criteria_value   = Column(String(100), nullable=True,  comment="핵심기준 값")

    parallel_import      = Column(String(50),  nullable=True,  comment="병행수입 가능여부 (O/X/수입이력 없음/확인필요)")
    recall_status        = Column(String(20),  nullable=True,  comment="리콜 이력 판정 (통과/탈락)")
    quality_label_status = Column(String(20),  nullable=True,  comment="품질·표시 판정 (통과/탈락)")
    legal_risk_status    = Column(String(20),  nullable=True,  comment="법적·평판 리스크 판정 (통과/탈락)")
    five_year_issue      = Column(String(20),  nullable=True,  comment="5년내 이슈 여부 (-/O/X)")
    notes                = Column(Text,        nullable=True,  comment="비고 (소송/리콜 등 상세)")

    rating               = Column(Numeric,     nullable=True,  comment="평점")
    review_count         = Column(Integer,     nullable=True,  comment="리뷰수")
    url                  = Column(Text,        nullable=True,  comment="상품 페이지 URL")
    image_url            = Column(Text,        nullable=True,  comment="상품 이미지 URL")
    verified_flag        = Column(String(50),  nullable=True,  comment="실측여부 (실측/추정 등)")

    __table_args__ = (
        UniqueConstraint("product_type", "retailer", "rank", name="uq_psi_type_retailer_rank"),
        Index("ix_psi_product_type", "product_type"),
        Index("ix_psi_retailer", "retailer"),
    )


class CountryItemAmount(Base):
    """
    국가별 품목별 수입금액 전체 (품목 검색 → 국가 리스트업 기능용).
    country_top_item과 달리 국가당 상위 10개로 제한하지 않고 전체 품목을 저장한다.
    """
    __tablename__ = "country_item_amount"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    country      = Column(String(100), nullable=False, comment="국가명")
    item_name    = Column(String(200), nullable=False, comment="수입품목명")
    amount_usd_k = Column(Numeric,     nullable=False, comment="수입금액 (천달러)")

    __table_args__ = (
        UniqueConstraint("country", "item_name", name="uq_cia_country_item"),
        Index("ix_cia_country", "country"),
        Index("ix_cia_item_name", "item_name"),
    )
