"""
models.py — DB 테이블 정의
테이블 구조는 Excel 컬럼 기반으로 설계.
실제 Excel 컬럼명이 달라질 경우 FIELD_MAP(importer.py)만 수정하면 됨.
"""
from sqlalchemy import (
    Column, Integer, String, Date, DateTime, Text, Index, UniqueConstraint, Numeric, LargeBinary, Boolean
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
    importers            = Column(Text,        nullable=True,  comment="병행수입 판정 근거 — factory별 수입업체 목록 (JSON, cross_check_results.json에서 이식)")
    recall_status        = Column(String(20),  nullable=True,  comment="리콜 이력 판정 (통과/탈락)")
    quality_label_status = Column(String(20),  nullable=True,  comment="품질·표시 판정 (통과/탈락)")
    legal_risk_status    = Column(String(20),  nullable=True,  comment="법적·평판 리스크 판정 (통과/탈락)")
    five_year_issue      = Column(String(20),  nullable=True,  comment="5년내 이슈 여부 (-/O/X)")
    notes                = Column(Text,        nullable=True,  comment="비고 (소송/리콜 등 상세)")

    rating               = Column(Numeric,     nullable=True,  comment="평점")
    review_count         = Column(Integer,     nullable=True,  comment="리뷰수")
    url                  = Column(Text,        nullable=True,  comment="상품 페이지 URL")
    image_url            = Column(Text,        nullable=True,  comment="상품 이미지 URL (raw 시트에 실제 호스팅 URL이 있는 유통사만)")
    image_data           = Column(LargeBinary, nullable=True,  comment="원본 엑셀에 삽입된 이미지 바이트 (image_url이 없는 유통사용 대체)")
    image_mime           = Column(String(50),  nullable=True,  comment="image_data의 MIME 타입")
    verified_flag        = Column(String(50),  nullable=True,  comment="실측여부 (실측/추정 등)")

    brand_group_key      = Column(String(200), nullable=True,  comment="브랜드 그룹핑 키 (동일 브랜드 묶음 정렬용, 정규화된 브랜드명)")
    product_group_key    = Column(String(200), nullable=True,  comment="동일 제품 그룹핑 키 (용량/유통사 무관 동일 제품 매칭, 브랜드 내부 정렬용)")

    hs_code               = Column(String(20),  nullable=True,  comment="HS코드 (품목분류, 10자리 — 원가 자동계산용, 리서치로 추정한 값)")
    hs_code_confidence    = Column(String(20),  nullable=True,  comment="HS코드 추정 신뢰도 (high/medium/very_low 등) — hs_code_importer.py 참고")

    # 아래 3개는 매 조회마다 계산하지 않고, tariff_rate 재적재/HS코드 변경
    # 시점에 미리 계산해서 저장해둔 캐시값이다 (cost_estimator.recompute_cost_estimates
    # 참고) — 조회 경로는 이 컬럼을 그대로 읽기만 해서 응답 속도를 지킨다.
    tariff_rate_pct            = Column(Numeric,     nullable=True,  comment="적용 관세율(%) — 캐시된 계산 결과")
    tariff_basis                = Column(String(100), nullable=True,  comment="적용 세율 근거 (캐시된 계산 결과)")
    estimated_landed_cost_krw  = Column(Numeric,     nullable=True,  comment="추정 착지원가(원) — 캐시된 계산 결과. landed_cost_is_per_kg=True면 상품 1개당이 아니라 1kg당 금액")
    landed_cost_is_per_kg      = Column(Boolean,      nullable=True,  comment="True면 estimated_landed_cost_krw가 unit 환산 실패로 1kg당 금액(원/kg)임 — 상품 1개당 총액이 아님")

    __table_args__ = (
        UniqueConstraint("product_type", "retailer", "rank", name="uq_psi_type_retailer_rank"),
        Index("ix_psi_product_type", "product_type"),
        Index("ix_psi_retailer", "retailer"),
        Index("ix_psi_hs_code", "hs_code"),
    )


class ProductSourcingExportCache(Base):
    """
    '원본 형식(유형별카드) 다운로드' .xlsx를 미리 만들어 캐싱해두는 테이블.
    Render 백엔드 인스턴스가 CPU/메모리가 넉넉하지 않아, 다운로드 요청마다
    ~7,000개 이미지를 담은 워크북을 즉석에서 만들면 응답이 너무 느리거나
    (실측 60초+ 후 연결 끊김) 메모리 부족으로 죽는 문제(502)가 실제로 있었다.
    그래서 상품 소싱 데이터가 재적재될 때(백필/업로드 후) 미리 한 번 만들어
    이 테이블에 저장해두고, 다운로드 요청은 이 캐시를 그대로 서빙만 한다.
    항상 단일 행(id=1)만 유지 — 갱신 시 UPDATE.
    """
    __tablename__ = "product_sourcing_export_cache"

    id           = Column(Integer, primary_key=True)
    file_data    = Column(LargeBinary, nullable=False)
    generated_at = Column(DateTime,    nullable=False)
    row_count    = Column(Integer,     nullable=True)


class ProductSourcingCrawlRun(Base):
    """
    아마존/이온몰(추후 월마트/샘스클럽) 자동 크롤링 1회차 기록.
    product_sourcing_item(메인페이지 실제 데이터, 수작업 검증 데이터 포함)은 이 크롤링으로
    전혀 건드리지 않는다 — 순수 이력(history) 보관용. 프론트 "크롤링 히스토리" 버튼에서 조회.
    """
    __tablename__ = "product_sourcing_crawl_run"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    run_at     = Column(DateTime, nullable=False, comment="크롤링 회차 업로드 시각")
    site_scope = Column(String(50), nullable=True, comment="이번 회차에 포함된 유통사 (예: amazon,aeon)")
    row_count  = Column(Integer, nullable=True, comment="이번 회차 총 상품 행 수")
    note       = Column(Text, nullable=True, comment="자유 메모 (성공/실패 건수 등)")


class ProductSourcingCrawlSnapshotItem(Base):
    """
    ProductSourcingCrawlRun 1회차에 속하는 상품 1건 (유형×유통사×순위).
    컬럼은 크롤링으로 채울 수 있는 필드만 — 원산지/병행수입/HS코드 등 수작업 검증 필드는 없음.
    """
    __tablename__ = "product_sourcing_crawl_snapshot_item"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    run_id           = Column(Integer, nullable=False, comment="product_sourcing_crawl_run.id")

    category         = Column(String(100), nullable=True, comment="대분류")
    product_type     = Column(String(300), nullable=False, comment="품목 유형")
    query_used       = Column(String(200), nullable=True, comment="크롤링에 사용한 카테고리 검색어")
    retailer         = Column(String(20),  nullable=False, comment="유통사 (amazon/aeon)")
    source_site      = Column(String(20),  nullable=True, comment="이온몰 국가 구분 (aeon-jp/aeon-my), 아마존은 null")
    rank             = Column(Integer,     nullable=False, comment="유통사(국가 구분 포함) 내 순위")

    brand            = Column(String(200), nullable=True)
    product_name_en  = Column(String(500), nullable=True)
    price_usd        = Column(Numeric,     nullable=True)
    rating           = Column(Numeric,     nullable=True)
    review_count     = Column(Integer,     nullable=True)
    url              = Column(Text,        nullable=True)
    image_url        = Column(Text,        nullable=True)

    __table_args__ = (
        Index("ix_pscsi_run_id", "run_id"),
        Index("ix_pscsi_product_type", "product_type"),
    )


class TariffRate(Base):
    """
    HS코드(품목번호)별 관세율표 — 관세청_품목번호별 관세율표(data.go.kr) 원본을 그대로 적재.
    rate_type: 'A'=기본세율, 'C'=WTO양허세율, 그 외(FEU1/FUS1/FCL1 등)는 FTA 협정세율
    코드 — 어느 협정/국가에 해당하는지는 fta_country_map.py에서 별도 매핑한다
    (이 표 자체에는 국가명이 없고 applies_country_group만 있음).
    """
    __tablename__ = "tariff_rate"

    id                     = Column(Integer, primary_key=True, autoincrement=True)
    hs_code                = Column(String(20),  nullable=False, comment="품목번호 (HS코드, 10자리)")
    rate_type              = Column(String(20),  nullable=False, comment="관세율구분 (A/C/FEU1/FUS1 등)")
    rate_pct               = Column(Numeric,     nullable=False, comment="관세율 (%)")
    applies_country_group  = Column(Integer,     nullable=True,  comment="적용국가구분 (원본 컬럼 그대로 보관, 참고용)")
    effective_from         = Column(Date,        nullable=True,  comment="적용개시일")
    effective_to           = Column(Date,        nullable=True,  comment="적용만료일")

    __table_args__ = (
        UniqueConstraint("hs_code", "rate_type", "effective_from", name="uq_tariff_hs_type_from"),
        Index("ix_tariff_hs_code", "hs_code"),
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
    weight_ton   = Column(Numeric,     nullable=True,  comment="수입중량 (톤) — MFDS cnd=wt 응답. 평균단가($/kg) = amount_usd_k / weight_ton")

    __table_args__ = (
        UniqueConstraint("country", "item_name", name="uq_cia_country_item"),
        Index("ix_cia_country", "country"),
        Index("ix_cia_item_name", "item_name"),
    )


class CrawlRunStatus(Base):
    """
    수입이력 크롤링(/api/crawl) 실행 이력 기록용 — GitHub Actions 워크플로우가
    언제 실제로 실행/완료됐는지를 대시보드에 보여주기 위해 항상 단일 행(id=1)만
    유지하며 크롤링 시작/종료마다 UPDATE.
    """
    __tablename__ = "crawl_run_status"

    id           = Column(Integer, primary_key=True)
    started_at   = Column(DateTime, nullable=True, comment="가장 최근 크롤링 시작 시각 (UTC)")
    finished_at  = Column(DateTime, nullable=True, comment="가장 최근 크롤링 완료 시각 (UTC)")
    status       = Column(String(20), nullable=True, comment="running / success / error")
    error        = Column(Text,     nullable=True, comment="실패 시 오류 메시지")
