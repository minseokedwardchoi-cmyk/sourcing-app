/**
 * 상품명에서 브랜드를 뽑는 공용 규칙. 83개 유형 전체로 확장하면서 카테고리별 불용어
 * 목록(예: 올리브유 전용 "Extra/Virgin/Olive/Oil...")은 다른 품목에서 오작동해서 폐기했다.
 *
 * 유통사별로 다른 규칙을 쓰는 이유(임의로 다르게 정한 게 아니라 실측 근거 있음, 메인페이지
 * 기존 7,397건 중 50% 샘플로 검증):
 * - 아마존/이온몰: 상품명이 "브랜드, 나머지 설명..." 형태로 브랜드 바로 뒤에 콤마가 오는
 *   경우가 많다 — 콤마 앞까지를 브랜드로 쓴다.
 * - 월마트/샘스클럽: 콤마가 브랜드가 아니라 **상품명 맨 끝, 용량 표기 앞**에 온다
 *   (예: "Smash Kitchen 100% Pure Avocado Oil, 25.4 fl oz" — 콤마는 "25.4 fl oz" 앞).
 *   그래서 콤마를 브랜드 경계 신호로 못 쓰고, 고정 단어수 폴백만 쓴다.
 *
 * 완전히 정확하진 않다(AI/외부 브랜드사전 없이 텍스트 패턴만으로는 한계가 있음 — 예:
 * "Skippy Creamy Peanut Butter"에서 진짜 브랜드는 "Skippy" 하나뿐인데 "Skippy Creamy"로
 * 뽑힐 수 있음) — 카테고리별 불용어 목록보다 유통사 무관하게 일관되고 실측으로 개선 확인된
 * 최선의 규칙이라는 정도로 받아들일 것.
 */

function splitWords(text) {
  return String(text || "").trim().split(/\s+/).filter(Boolean);
}

/**
 * 아마존/이온몰용 — 콤마/파이프가 나오면 그 앞까지(최대 maxWords 단어), 없으면 앞
 * fallbackWords개 단어.
 */
export function extractBrandCommaOrWords(title, { maxWords = 3, fallbackWords = 2 } = {}) {
  const clean = String(title || "").trim();
  if (!clean) return null;

  const boundaryIdx = clean.search(/[,|]/);
  if (boundaryIdx > 0) {
    const head = clean.slice(0, boundaryIdx).trim();
    const words = splitWords(head);
    if (words.length >= 1) {
      return words.slice(0, maxWords).join(" ");
    }
  }

  const words = splitWords(clean);
  return words.slice(0, fallbackWords).join(" ") || null;
}

/** 월마트/샘스클럽용 — 콤마 신호를 안 쓰고 항상 앞 wordCount개 단어만. */
export function extractBrandFixedWords(title, wordCount = 2) {
  const words = splitWords(title);
  return words.slice(0, wordCount).join(" ") || null;
}
