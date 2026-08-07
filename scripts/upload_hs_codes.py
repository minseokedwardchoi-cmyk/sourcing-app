#!/usr/bin/env python3
"""
scripts/upload_hs_codes.py
상품별(유형×유통사×순위) HS코드 리서치 결과 Excel을 백엔드의
/api/upload-hs-codes 엔드포인트로 업로드한다.

confidence='high'인 행은 hs_code 그대로, 'medium'은 hs_code_confidence='medium'으로
같이 저장(프론트에서 "(검토 필요)" 표시), 'low'/'very_low'/미상 신뢰도는
반영하지 않는다 (skipped로 집계).

사용법:
  python3 scripts/upload_hs_codes.py 파일경로.xlsx
  python3 scripts/upload_hs_codes.py 파일경로.xlsx --url https://sourcing-backend-ucp5.onrender.com
"""
import argparse
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://sourcing-backend-ucp5.onrender.com"
DEFAULT_TIMEOUT = 300


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="업로드할 .xlsx 파일 경로")
    parser.add_argument("--url", default=DEFAULT_URL, help="백엔드 base URL")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()

    with open(args.file, "rb") as f:
        file_bytes = f.read()

    boundary = "----HsCodeUpload"
    filename = args.file.split("/")[-1]
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
    ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{args.url}/api/upload-hs-codes",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    print(f"업로드 중... ({len(file_bytes) / 1024 / 1024:.1f} MB → {args.url})")
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            print(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"실패 ({e.code}): {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
