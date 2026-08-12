/**
 * 구글 번역의 비공식(무료, API 키 불필요) 엔드포인트로 텍스트를 번역한다.
 * 실패하면 null을 반환한다 (호출부에서 원문을 그대로 쓰면 됨).
 */
async function translate(text, targetLang) {
  if (!text) return null;
  const url = `https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=${targetLang}&dt=t&q=${encodeURIComponent(
    text
  )}`;
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(8000) });
    if (!res.ok) return null;
    const data = await res.json();
    return data[0].map((chunk) => chunk[0]).join("");
  } catch {
    return null;
  }
}

export async function translateToEnglish(text) {
  return translate(text, "en");
}

export async function translateToJapanese(text) {
  return translate(text, "ja");
}
