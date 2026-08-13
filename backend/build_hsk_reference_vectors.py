"""
build_hsk_reference_vectors.py — HSK 참조표(hsk_reference_20260101.csv, 11,327개
HS10코드) 전체를 로컬 임베더로 벡터화해서 backend/reference_data/hsk_reference_vectors.npy에
저장한다.

관세청이 연 1회(1/1) 새 HSK표를 내는 것과 같은 주기로만 다시 계산하면 됨 —
hs_code_estimate_gemini.py는 ensure_reference_vectors()로 이미 계산된 결과가 있으면
그대로 로드만 하고(빠름), 없으면(최초 1회, 또는 표가 갱신돼서 지웠을 때) 이 스크립트
로직으로 새로 계산한다. GitHub Actions에서는 actions/cache로 이 결과물을 캐싱해서
매 회차 재계산을 피한다(hs_code_estimation.yml 참고).

수동 실행: python build_hsk_reference_vectors.py [--force]
"""
from __future__ import annotations

import argparse
import csv
import os
import time

import numpy as np

from local_text_embedder import embed_texts

REFERENCE_CSV = "reference_data/hsk_reference_20260101.csv"
OUT_NPY = "reference_data/hsk_reference_vectors.npy"
OUT_CODES = "reference_data/hsk_reference_codes.csv"
BATCH = 32


def _build(reference_csv: str = REFERENCE_CSV, out_npy: str = OUT_NPY, out_codes: str = OUT_CODES):
    with open(reference_csv, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    print(f"{len(rows)}개 HS10코드 로드")

    # 한글+영문 계층명을 합쳐서 임베딩 대상 텍스트로 사용 — 크롤링 상품명이 영어라
    # 영문 쪽 매칭이 주력이지만, 한글 품목명도 같이 넣어두면 다국어 모델이 의미
    # 유사도를 더 잘 잡는 경우가 있어(관세청 용어가 영문보다 한글이 더 구체적인 경우도 있음).
    texts = [f"{r['name_kr']} | {r['name_en']}" for r in rows]

    t0 = time.time()
    vectors = np.zeros((len(texts), 384), dtype=np.float32)
    for start in range(0, len(texts), 500):
        chunk = texts[start:start + 500]
        vectors[start:start + len(chunk)] = embed_texts(chunk, batch_size=BATCH)
        print(f"  {start + len(chunk)}/{len(texts)} ({time.time()-t0:.0f}s)")

    np.save(out_npy, vectors)
    with open(out_codes, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["hs_code", "name_kr", "name_en"])
        for r in rows:
            w.writerow([r["hs_code"], r["name_kr"], r["name_en"]])

    print(f"완료: {out_npy} ({vectors.shape}), {time.time()-t0:.0f}초")


def ensure_reference_vectors(force: bool = False) -> tuple[np.ndarray, list[dict]]:
    """(벡터 배열, [{hs_code,name_kr,name_en}, ...]) — 없으면 계산해서 저장, 있으면 로드만."""
    if force or not (os.path.exists(OUT_NPY) and os.path.exists(OUT_CODES)):
        _build()
    vectors = np.load(OUT_NPY)
    with open(OUT_CODES, encoding="utf-8") as f:
        codes = list(csv.DictReader(f))
    if len(codes) != vectors.shape[0]:
        # 참조표 CSV와 저장된 코드 목록이 어긋나면(수동으로 CSV만 갱신한 경우 등) 다시 계산
        _build()
        vectors = np.load(OUT_NPY)
        with open(OUT_CODES, encoding="utf-8") as f:
            codes = list(csv.DictReader(f))
    return vectors, codes


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="이미 있어도 강제로 재계산")
    args = parser.parse_args()
    ensure_reference_vectors(force=args.force)
