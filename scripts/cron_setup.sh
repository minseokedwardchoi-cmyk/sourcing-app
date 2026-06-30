#!/bin/bash
# 서버에서 한 번만 실행하면 cron 등록 완료
# 사용법: bash scripts/cron_setup.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="$(which python3)"
LOG_FILE="$SCRIPT_DIR/logs/crawl.log"

# 의존성 설치
pip3 install -r "$SCRIPT_DIR/requirements.txt" -q
playwright install chromium

mkdir -p "$SCRIPT_DIR/logs"

# 매일 새벽 2시 실행 (한국 시간 기준 서버에 KST 설정 가정)
CRON_LINE="0 2 * * * cd $PROJECT_DIR && BACKEND_URL=http://localhost:8000 $PYTHON $SCRIPT_DIR/crawl_and_upload.py >> $LOG_FILE 2>&1"

# 기존 동일 라인 제거 후 추가
(crontab -l 2>/dev/null | grep -v "crawl_and_upload.py"; echo "$CRON_LINE") | crontab -

echo "Cron 등록 완료:"
crontab -l | grep crawl_and_upload
echo ""
echo "초기 백필 실행 (6/1~6/29):"
echo "  python3 $SCRIPT_DIR/crawl_and_upload.py --start 2026-06-01 --end 2026-06-29"
