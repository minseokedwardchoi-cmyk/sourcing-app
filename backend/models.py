"""
models.py — DB 테이블 정의
테이블 구조는 Excel 컬럼 기반으로 설계.
실제 Excel 컬럼명이 달라질 경우 FIELD_MAP(importer.py)만 수정하면 됨.
"""
from sqlalchemy import (
    Column, Integer, String, Date, Text, Index, UniqueConstraint, Numeric
)
from sqlalchemy.dialects.postgresql import TSVECTOR
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

    # ── 전문 검색용 벡터 (PostgreSQL FTS) ─────────────────
    search_vector    = Column(TSVECTOR,     nullable=True)

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
        # FTS 인덱스
        Index("ix_search_vector", "search_vector", postgresql_using="gin"),
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
