# Global Factory Sourcing Dashboard

해외 제조업체 소싱 대시보드 — FastAPI + PostgreSQL + React

## 아키텍처

```
sourcing-app/
├── backend/          # FastAPI 서버
│   ├── main.py       # API 엔드포인트
│   ├── models.py     # DB 테이블 (SQLAlchemy)
│   ├── schemas.py    # API 응답 스키마 (Pydantic)
│   ├── database.py   # DB 연결 설정
│   ├── importer.py   # Excel → DB 적재 (FIELD_MAP 여기서 관리)
│   └── requirements.txt
└── frontend/         # React + Vite
    ├── src/
    │   ├── App.jsx   # 3페이지 대시보드
    │   ├── api.js    # API 호출 모듈
    │   └── main.jsx
    └── package.json
```

---

## 로컬 실행 방법

### 1. PostgreSQL 실행
```bash
# Docker로 빠르게 실행
docker run -d \
  --name sourcing-db \
  -e POSTGRES_USER=user \
  -e POSTGRES_PASSWORD=password \
  -e POSTGRES_DB=sourcing_db \
  -p 5432:5432 \
  postgres:16
```

### 2. 백엔드 실행
```bash
cd backend

# 의존성 설치
pip install -r requirements.txt

# 환경변수 설정
cp .env.example .env
# .env 파일에서 DATABASE_URL 수정

# 서버 실행 (자동으로 테이블 생성)
uvicorn main:app --reload --port 8000
```

### 3. 프론트엔드 실행
```bash
cd frontend

# 의존성 설치
npm install

# 환경변수 설정
cp .env.example .env
# .env 파일에서 VITE_API_URL 확인 (로컬은 기본값 사용 가능)

# 개발 서버 실행
npm run dev
# → http://localhost:5173 접속
```

---

## Excel 데이터 업로드

대시보드 우측 상단 **"📤 Excel 업로드"** 버튼 클릭 → .xlsx 파일 선택

### 필수 Excel 컬럼명 (헤더 행)

| 컬럼명 | 설명 | 예시 |
|--------|------|------|
| 구분 | PB/NB/부자재 등 | PB |
| MC | 상품 카테고리 | 과자/제과 |
| SKU명 | 상품명 | KS 다크초콜릿 70% |
| OEM/수입 여부 | OEM 또는 수입 | OEM |
| 수입업체 | 수입업체명 | (주)이마트 |
| 제조사명 | 해외 제조사명 | Barry Callebaut AG |
| 해외제조업소 | 실제 공장명 | Barry Callebaut Belgium NV |
| 제조국 | 국가명 | 벨기에 |
| 이메일 | 연락처 이메일 | sourcing@example.com |
| 홈페이지 | 홈페이지 URL | https://example.com |
| 수입일자 | 날짜 (YYYY-MM-DD) | 2024-03-15 |
| OEM 가능성 | OEM 가능/불가/문의가능 | OEM 가능 |
| OEM 메모 | 상세 메모 | 대용량 OEM 전문 |
| 인증서 | 콤마 구분 | ISO 22000, HACCP |

> 컬럼명이 다를 경우: `backend/importer.py`의 `FIELD_MAP` 딕셔너리만 수정

### 경쟁사명 자동 정규화
`(주)이마트`, `EMART`, `이마트` → 자동으로 `이마트`로 통합
`backend/importer.py`의 `COMPETITOR_MAP`에서 추가/수정 가능

---

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | /api/sku-history | SKU 이력 집계 (메인 테이블) |
| GET | /api/sku/{sku_name}/factories | SKU 취급 제조사 목록 |
| GET | /api/manufacturer | 제조사 상세 정보 |
| POST | /api/upload | Excel 업로드 |
| GET | /api/stats | DB 규모 통계 |
| GET | /health | 헬스체크 |

API 문서 (Swagger): http://localhost:8000/docs

---

## 배포 가이드

### Option A: Docker Compose (권장)
```bash
# docker-compose.yml 참고
docker compose up -d
```

### Option B: 분리 배포
- **백엔드**: Railway / Render / EC2 에 uvicorn 실행
- **프론트엔드**: Vercel / Netlify에 `npm run build` 후 배포
- **DB**: Supabase / RDS / Railway PostgreSQL

### 환경변수 (배포 시 필수)
```
# Backend
DATABASE_URL=postgresql+asyncpg://user:pw@host:5432/sourcing_db
ALLOWED_ORIGINS=https://your-frontend-domain.com

# Frontend
VITE_API_URL=https://your-backend-domain.com
```
