# 무료 Hybrid Search 배포

이 구성은 기존 `intfloat/multilingual-e5-small`의 INT8 ONNX 실행만 두 번째
Render 서비스로 분리한다. 기존 384차원 pgvector 데이터는 유지한다.

## 1. 임베딩 Render 서비스 생성

1. Render에서 새 **Web Service**를 만든다.
2. 기존 GitHub 저장소를 연결하고 Root Directory를 `embedding-space`로 지정한다.
3. Runtime은 Docker, Instance Type은 Free를 선택한다.
4. Environment에 `EMBEDDING_SERVICE_TOKEN`을 추가한다.
   값은 충분히 긴 임의 문자열로 만들고 외부에 공개하지 않는다.
5. 빌드가 끝난 뒤 임베딩 서비스의 `/health`에서
   `{"status":"ok"}` 응답을 확인한다.

서비스 URL은 공개여도 임베딩 엔드포인트는 위 Secret이 없으면 401을 반환한다.

## 2. Render 환경변수

기존 sourcing-backend에 다음 값을 설정한다.

```text
HYBRID_SEARCH_ENABLED=true
EMBEDDING_PROVIDER=remote
LOCAL_EMBEDDING_MODEL=intfloat/multilingual-e5-small
EMBEDDING_DIMENSIONS=384
EMBEDDING_SERVICE_URL=https://<embedding-service>.onrender.com
EMBEDDING_SERVICE_TOKEN=<Space에 넣은 것과 동일한 Secret>
EMBEDDING_SERVICE_TIMEOUT=30
DB_POOL_SIZE=3
DB_MAX_OVERFLOW=2
```

`EMBEDDING_SERVICE_URL` 끝에는 `/embed/query`를 붙이지 않는다.

## 3. 확인 순서

1. 임베딩 서비스의 `/health`가 `ok`인지 확인한다.
2. Render를 재배포한다.
3. Render `/health`를 확인한다.
4. 대시보드에서 실제 검색어를 입력한다.
5. `/api/search-hybrid` 응답에서 `hybrid_enabled: true`,
   `semantic_error: null`인지 확인한다.

임베딩 서비스 호출에 실패해도 현재 백엔드는 기존 exact 검색으로 fallback한다.
시연 전에는 임베딩 서비스 `/health`를 한 번 열어 깨워 둔다.
