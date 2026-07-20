#!/bin/bash
# 제조사 대표 이메일 크롤러(crawl_manufacturer_emails.py)를 매주 cron으로 등록.
# crawl_and_upload.py(MFDS 수입이력, 매일)와 별도 스크립트로 두는 이유:
#   - Playwright/Chromium이 필요 없어 의존성이 더 가벼움
#   - 외부 사이트(제조사 홈페이지, 알리바바 등) 크롤링이라 매일 돌릴 필요는 없고,
#     오히려 너무 자주 돌리면 같은 사이트에 불필요한 부하만 줌
# 사용법: bash scripts/cron_setup_email_crawl.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="$(which python3)"
LOG_FILE="$SCRIPT_DIR/logs/email_crawl.log"

pip3 install -r "$SCRIPT_DIR/requirements.txt" -q

mkdir -p "$SCRIPT_DIR/logs"

# 매주 일요일 새벽 3시 실행 (한국 시간 기준 서버에 KST 설정 가정)
# BACKEND_URL은 실제 배포된 백엔드 주소로 바꿔서 실행하세요.
CRON_LINE="0 3 * * 0 cd $PROJECT_DIR && BACKEND_URL=\${EMAIL_CRAWL_BACKEND_URL:-http://localhost:8000} $PYTHON $SCRIPT_DIR/crawl_manufacturer_emails.py --limit 500 >> $LOG_FILE 2>&1"

# 기존 동일 라인 제거 후 추가
(crontab -l 2>/dev/null | grep -v "crawl_manufacturer_emails.py"; echo "$CRON_LINE") | crontab -

echo "Cron 등록 완료:"
crontab -l | grep crawl_manufacturer_emails
echo ""
echo "BACKEND_URL을 실제 프로덕션 백엔드 주소로 지정하려면 crontab -e 에서"
echo "EMAIL_CRAWL_BACKEND_URL=https://<실제-백엔드-주소> 를 CRON_LINE 앞에 추가하세요."
echo ""
echo "먼저 한 번 결과만 확인하려면 (DB에 반영 안 함):"
echo "  BACKEND_URL=https://<실제-백엔드-주소> python3 $SCRIPT_DIR/crawl_manufacturer_emails.py --limit 50 --dry-run"
