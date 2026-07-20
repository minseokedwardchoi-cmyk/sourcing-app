#!/usr/bin/env python3
"""
scripts/research_distributors.py
제조사별 "미국/유럽/일본/중국 주요 유통사 납품 이력" 조사 프로토타입 스크립트.

Claude API의 웹서치 도구(web_search)를 이용해 제조사가 아래 주요 유통사에
직접 유통되는지 확인하고, 확인되면 유통사명 + 매출을, 확인이 안 되면 대신
진출국(export_countries) 목록을 채워서 CSV로 출력한다.

아직 백엔드/프론트엔드 시스템에는 연결하지 않은 독립 테스트 스크립트다.
결과 품질을 먼저 확인한 뒤 실제 기능으로 옮길지 판단하기 위한 용도.

필요 패키지:
  pip install anthropic pandas

환경변수:
  ANTHROPIC_API_KEY 필요 (Claude API 키)

사용법:
  python3 scripts/research_distributors.py manufacturers.csv --out result.csv
  python3 scripts/research_distributors.py manufacturers.csv --out result.csv --model claude-opus-4-8
  python3 scripts/research_distributors.py --single "BIERES DE CHIMAY S.A." "벨기에"

입력 CSV는 제조사명(또는 해외제조업소)과 국가 컬럼을 포함해야 한다.
컬럼명은 아래 후보 중 하나면 자동으로 인식한다 (대소문자 무시):
  이름: 제조사명, 제조사, 해외제조업소, 해외 제조업소, factory, manufacturer
  국가: 국가, 제조국, 세소국, country
"""

import argparse
import csv
import json
import sys
import time

import anthropic

_NAME_COLS = ["제조사명", "제조사", "해외제조업소", "해외 제조업소", "factory", "manufacturer"]
_COUNTRY_COLS = ["국가", "제조국", "세소국", "country"]

_MAJOR_DISTRIBUTORS_KO = """\
주요 유통사 목록 (아래 목록에 있는 곳만 "주요 유통사"로 인정합니다):
- 미국: Walmart, Costco, Kroger, Target, Sam's Club
- 유럽: Carrefour, Tesco, Aldi, Lidl, Metro
- 일본: AEON(이온), Seven & i Holdings(세븐일레븐/이토요카도), Life Corporation(라이프)
- 중국: Alibaba/Tmall, JD.com(京东), Hema(盒马), Costco China, Yonghui(永辉)
"""

_SYSTEM_PROMPT = f"""당신은 해외 제조사가 미국/유럽/일본/중국의 주요 유통사에 직접 납품한 이력이
있는지 조사하는 리서치 어시스턴트입니다.

{_MAJOR_DISTRIBUTORS_KO}

증거 판정 기준:
- 인정: 뉴스 기사, 소비자 블로그/리뷰(실제 구매·목격을 구체적으로 언급), 해당 유통사
  공식 홈페이지의 자체(1st-party) 상품 페이지
- 불인정: 아마존/월마트 등 오픈마켓의 3rd-party 판매자 리스팅만 있는 경우. 이런
  리스팅만 발견되면 found를 false로 처리하세요 (독립적인 뉴스·블로그로 별도
  교차 확인되지 않는 한).
- 확실한 증거가 없으면 found=false로 표시하고, 대신 이 제조사가 수출/진출한
  것으로 확인되는 국가 목록(export_countries)을 조사해서 채우세요.
- 절대로 추측하지 마세요. 실제 검색 결과에 근거가 있을 때만 답하고, 근거가
  약하면 confidence를 low로 낮추세요.
- distributor_revenue는 found가 true이고 해당 유통사의 공개된 매출 정보를
  찾은 경우에만 채우세요 (연도와 통화 단위를 함께 표기).

웹 검색을 수행한 뒤, 정확히 지정된 JSON 스키마에 맞는 결과만 출력하세요."""

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "region": {"anyOf": [{"type": "string", "enum": ["US", "EU", "JP", "CN"]}, {"type": "null"}]},
        "distributor_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "distributor_revenue": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "evidence_type": {"type": "string", "enum": ["news_article", "blog_post", "official_retailer_page", "none"]},
        "evidence_url": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "export_countries": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "found", "region", "distributor_name", "distributor_revenue",
        "evidence_type", "evidence_url", "confidence", "export_countries",
    ],
    "additionalProperties": False,
}


def _find_col(fieldnames: list[str], candidates: list[str]) -> str | None:
    lowered = {f.lower(): f for f in fieldnames}
    for c in candidates:
        if c.lower() in lowered:
            return lowered[c.lower()]
    return None


def load_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        name_col = _find_col(fieldnames, _NAME_COLS) or fieldnames[0]
        country_col = _find_col(fieldnames, _COUNTRY_COLS) or (fieldnames[1] if len(fieldnames) > 1 else None)
        rows = []
        for r in reader:
            rows.append({
                "manufacturer": (r.get(name_col) or "").strip(),
                "country": (r.get(country_col) or "").strip() if country_col else "",
            })
        return [r for r in rows if r["manufacturer"]]


def research_one(client: anthropic.Anthropic, model: str, manufacturer: str, country: str,
                  max_searches: int) -> dict:
    user_text = f"제조사명: {manufacturer}\n제조국: {country or '불명'}\n" \
                "이 제조사가 위 목록의 주요 유통사에 직접 납품한 이력을 조사해주세요."

    messages = [{"role": "user", "content": user_text}]
    for _ in range(3):  # pause_turn 발생 시 재시도 (서버측 검색 루프가 10회 제한에 걸린 경우)
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}],
            output_config={"format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA}},
            messages=messages,
        )
        if response.stop_reason == "pause_turn":
            messages = [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": response.content},
            ]
            continue
        if response.stop_reason == "refusal":
            return {"found": False, "region": None, "distributor_name": None,
                    "distributor_revenue": None, "evidence_type": "none", "evidence_url": None,
                    "confidence": "low", "export_countries": [], "_error": "refusal"}
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            return {"found": False, "region": None, "distributor_name": None,
                    "distributor_revenue": None, "evidence_type": "none", "evidence_url": None,
                    "confidence": "low", "export_countries": [], "_error": "no_text_block"}
        return json.loads(text)

    return {"found": False, "region": None, "distributor_name": None,
            "distributor_revenue": None, "evidence_type": "none", "evidence_url": None,
            "confidence": "low", "export_countries": [], "_error": "pause_turn_exhausted"}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_csv", nargs="?", help="제조사명/국가 컬럼을 포함한 CSV 파일 경로")
    parser.add_argument("--single", nargs=2, metavar=("제조사명", "국가"), help="CSV 없이 하나만 테스트")
    parser.add_argument("--out", default="distributor_research_result.csv", help="결과 저장 경로")
    parser.add_argument("--model", default="claude-opus-4-8", help="사용할 Claude 모델")
    parser.add_argument("--max-searches", type=int, default=5, help="제조사 1건당 최대 웹서치 횟수")
    parser.add_argument("--sleep", type=float, default=1.0, help="요청 간 대기 시간(초)")
    args = parser.parse_args()

    if not args.single and not args.input_csv:
        parser.error("input_csv 또는 --single 중 하나는 필수입니다.")

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 환경변수 사용

    if args.single:
        manufacturer, country = args.single
        rows = [{"manufacturer": manufacturer, "country": country}]
    else:
        rows = load_rows(args.input_csv)
        print(f"[1/2] {len(rows)}건 로드 완료")

    fieldnames = ["manufacturer", "country", "found", "region", "distributor_name",
                  "distributor_revenue", "evidence_type", "evidence_url", "confidence",
                  "export_countries", "error"]

    with open(args.out, "w", newline="", encoding="utf-8-sig") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=fieldnames)
        writer.writeheader()

        for i, row in enumerate(rows, 1):
            print(f"[조사 중 {i}/{len(rows)}] {row['manufacturer']} ({row['country'] or '국가 불명'})")
            try:
                result = research_one(client, args.model, row["manufacturer"], row["country"], args.max_searches)
            except Exception as e:
                print(f"      실패: {e}")
                result = {"found": False, "region": None, "distributor_name": None,
                          "distributor_revenue": None, "evidence_type": "none", "evidence_url": None,
                          "confidence": "low", "export_countries": [], "_error": str(e)}

            writer.writerow({
                "manufacturer": row["manufacturer"],
                "country": row["country"],
                "found": result.get("found"),
                "region": result.get("region"),
                "distributor_name": result.get("distributor_name"),
                "distributor_revenue": result.get("distributor_revenue"),
                "evidence_type": result.get("evidence_type"),
                "evidence_url": result.get("evidence_url"),
                "confidence": result.get("confidence"),
                "export_countries": ", ".join(result.get("export_countries") or []),
                "error": result.get("_error", ""),
            })
            out_f.flush()

            if i < len(rows):
                time.sleep(args.sleep)

    print(f"[2/2] 완료 — 결과가 {args.out}에 저장되었습니다.")


if __name__ == "__main__":
    main()
