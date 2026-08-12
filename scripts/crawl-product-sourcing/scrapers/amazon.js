import fs from "node:fs";
import path from "node:path";

// oliveoil-scraper(D:\AI 프로젝트\유통사크롤러\...\oliveoil-scraper, r.jina.ai 리더 프록시 방식)의
// scrapers/amazon.js를 그대로 가져오되, reviewCount 추출 로직만 추가했다.
// (원본은 이미지-앵커 파싱 경로에서 reviewCount를 항상 null로 두고 있었는데, Amazon 검색결과는
// 거의 항상 이미지가 있어서 이 경로가 사실상 유일한 경로로 쓰인다 — 즉 원본 그대로면 리뷰수가
// 거의 항상 비게 된다. 이번 작업은 리뷰수가 필수 필드라 이 부분만 보강함.)
// ⚠️ 이 reviewCount 정규식은 실제 r.jina.ai 마크다운을 이 세션에서 직접 확인 못 하고 작성한
// 추정 기반이다. 실행 후 output/debug-amazon-*.md 를 열어서 리뷰수가 계속 비면, 그 파일의
// 실제 텍스트 구조를 보고 정규식을 다시 맞춰야 한다.

// 미국 배송지(zip) 쿠키 전달 — 배송지가 안 잡힌 채로 검색하면 베스트셀러 정렬/검색결과가
// 실제 미국 사용자가 보는 것과 달라진다는 게 실측으로 확인됨. r.jina.ai는 X-Set-Cookie
// 헤더로 대상 사이트(amazon.com)에 쿠키를 실어서 요청해주는 기능을 지원한다(문서 기준) —
// 단 이 환경은 r.jina.ai 자체가 막혀있어 직접 검증은 못 했다. 사용법: 실제 크롬에서
// amazon.com 배송지를 미국 zip(예: 90210)으로 설정한 뒤, DevTools > Network에서 아무
// amazon.com 요청이나 "Copy as cURL" → -H 'cookie: ...' 값을 이 프로젝트 루트의
// amazon-cookie.txt 파일에 그대로 붙여넣으면 자동으로 실린다 (환경변수 AMAZON_COOKIE로도 가능).
function loadAmazonCookie() {
  if (process.env.AMAZON_COOKIE) return process.env.AMAZON_COOKIE.trim();
  try {
    const text = fs.readFileSync(path.join(process.cwd(), "amazon-cookie.txt"), "utf-8").trim();
    return text || null;
  } catch {
    return null;
  }
}
const AMAZON_COOKIE = loadAmazonCookie();
let cookieWarningShown = false;
function warnIfNoCookie() {
  if (AMAZON_COOKIE || cookieWarningShown) return;
  cookieWarningShown = true;
  console.warn(
    "  [amazon] 경고: amazon-cookie.txt(미국 배송지 쿠키)가 없어서 배송지 미설정 상태로 검색합니다 — README 참고."
  );
}

const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();

const GENERIC_EXCLUDED = [
  "shampoo", "conditioner", "serum", "moisturizer", "cologne", "perfume",
  "hair oil", "skin oil", "body oil", "cleansing oil", "lotion", "detergent",
  "air freshener", "candle", "phone case",
];

const isRelevantProduct = (title) => {
  const text = clean(title).toLowerCase();
  return text && !GENERIC_EXCLUDED.some((word) => text.includes(word));
};

function looksLikeTitle(label) {
  if (/\$\s?\d/.test(label) || /Typical:/i.test(label)) return false;
  const letters = (label.match(/[A-Za-z]/g) || []).length;
  return letters / label.length > 0.5;
}

const SORT_MAP = {
  featured: null,
  "best-sellers": "exact-aware-popularity-rank",
  "price-asc": "price-asc-rank",
  "price-desc": "price-desc-rank",
  reviews: "review-rank",
  newest: "date-desc-rank",
};

function extractSize(title) {
  return clean(title).match(/\d+(?:\.\d+)?\s?(?:fl\.?\s?oz|fluid ounces?|ml|milliliters?|l|liters?|litres?|oz|ounces?|g|kg)\b/i)?.[0] || null;
}

function baseTitleKey(title) {
  return clean(title)
    .toLowerCase()
    .replace(/\d+(?:\.\d+)?\s?(?:fl\.?\s?oz|fluid ounces?|ml|milliliters?|l|liters?|litres?|oz|ounces?|g|kg)\b/gi, "")
    .replace(/[()]/g, "")
    .trim();
}

// "4.7 out of 5 stars" 근처에서 리뷰수를 찾는다. Amazon 검색결과 마크다운에서 흔한 두 형태를 시도:
// (1) "...out of 5 stars 2,341" 처럼 별점 바로 뒤에 숫자가 붙는 형태
// (2) "(2,341)" 처럼 괄호로 감싸진 형태 — 단, "($1.05/fl oz)" 같은 단가 표기와 헷갈리지 않게
//     바로 앞에 "$"가 없고 바로 뒤에 "/"가 오지 않는 경우만 인정
// "40.5K"/"1,234"처럼 K/M 접미사가 붙은 문자열은 Number()로 바로 변환하면 NaN이 나온다
// (0보다 큰지 검증하려다 매번 걸러지는 버그가 있었음) — K/M을 실제 배수로 환산해서 검증한다.
function looksLikePositiveCount(text) {
  const m = String(text).match(/^([\d,]+(?:\.\d+)?)\s*([KM])?$/i);
  if (!m) return false;
  const base = Number(m[1].replace(/,/g, ""));
  return Number.isFinite(base) && base > 0;
}

export function extractReviewCount(segment, ratingMatch) {
  const after = segment.slice(ratingMatch.index + ratingMatch[0].length, ratingMatch.index + ratingMatch[0].length + 300);
  // 1) "[(40.5K)](url)" — 실측 확인된 실제 형태: 대괄호 링크 안에 괄호로 한 번 더 감싸져 있음
  //    (평점/가격과 같은 "제목/링크" 패턴일 거라 짐작했던 것과 실제 구조가 달랐음).
  //    ⚠️ url 부분(아마존 추적 파라미터 포함)이 300자 슬라이스보다 길어서 닫는 ")"가 안에 없는
  //    경우가 실측으로 확인됨 — url의 닫는 괄호까지 요구하지 않고 여는 "(" 까지만 확인한다.
  const bracket = after.match(/\[\(?([\d,]+(?:\.\d+)?[KM]?)\)?\]\(/);
  if (bracket && looksLikePositiveCount(bracket[1])) return bracket[1];
  // 2) "4.7 out of 5 stars 2,341" 처럼 별점 뒤에 숫자가 바로 붙는 형태
  const inline = after.match(/^\)?\s*([\d,]+(?:\.\d+)?[KM]?)\b/);
  if (inline && looksLikePositiveCount(inline[1])) return inline[1];
  // 3) "(2,341)" 괄호 형태(대괄호 없이) — 가격/단가 표기($.../fl oz)와 헷갈리지 않게 방어
  const paren = after.match(/\(([\d,]+(?:\.\d+)?[KM]?)\)(?!\s*\/)/);
  if (paren && looksLikePositiveCount(paren[1]) && !/\$\s*[\d.,]*\s*$/.test(after.slice(0, paren.index))) {
    return paren[1];
  }
  return null;
}

export function parseAmazonMarkdown(markdown, pageNumber) {
  const imagePattern = /!\[Image\s+\d+:\s*([^\]]+)\]\(([^)]+)\)/g;
  const imageMatches = Array.from(markdown.matchAll(imagePattern));
  const imageItems = [];
  for (let index = 0; index < imageMatches.length; index += 1) {
    const match = imageMatches[index];
    let title = clean(match[1]);
    const imageUrl = match[2] || null;
    const start = match.index + match[0].length;
    const nextStart = imageMatches[index + 1]?.index ?? markdown.length;
    const segment = markdown.slice(start, Math.min(nextStart, start + 12000));
    const rawLabels = Array.from(segment.matchAll(/\[([^\]]{20,1000})\]\([^)]+\)/g)).map(
      (labelMatch) => clean(labelMatch[1].replace(/[*_`#]/g, " "))
    );
    if (rawLabels.some((label) => /^sponsored\b/i.test(label))) continue;

    const labels = rawLabels
      .filter((label) => !/out of 5 stars|^Image/i.test(label))
      .filter(looksLikeTitle);
    const fuller = labels.sort((a, b) => b.length - a.length)[0];
    if (fuller && fuller.length > title.length) {
      const marker = fuller.slice(0, Math.min(24, fuller.length));
      const repeatedAt = fuller.indexOf(marker, marker.length);
      title = clean(repeatedAt > 0 ? fuller.slice(0, repeatedAt) : fuller);
    }
    // 아마존 카드 마크다운은 브랜드를 제목 링크와 별도의 "## 브랜드명" 헤딩(링크 아님)으로
    // 렌더링하는 경우가 있다 — 위 rawLabels는 [텍스트](링크) 형태만 스캔해서 이 헤딩을 놓치고,
    // 이미지 alt 텍스트(브랜드 포함하지만 "..."로 잘림)가 위에서 "fuller"(브랜드 없는 완전한
    // 제목 링크)로 덮어써지면서 브랜드가 통째로 사라지는 사고가 실측으로 확인됨(Pompeian/
    // Partanna 등 여러 건). "## 브랜드\n\n## [제목]" 패턴을 찾아 안 붙어있으면 다시 붙인다.
    const brandHeadingMatch = segment.match(/^##\s+([^\n[][^\n]{0,60})\n\n##\s+\[/m);
    const brandHeading = brandHeadingMatch ? clean(brandHeadingMatch[1]) : null;
    if (brandHeading && !title.toLowerCase().startsWith(brandHeading.toLowerCase())) {
      title = clean(`${brandHeading} ${title}`);
    }
    const ratingMatch = segment.match(/([0-5](?:\.\d)?)\s+out of 5 stars/i);
    const priceMatch = segment.match(/\$\s*([\d,]+(?:\.\d{2})?)/);
    if (!ratingMatch || !priceMatch || !isRelevantProduct(title)) continue;
    const asin = segment.match(/\/(?:dp|gp\/aw\/d)\/(B[0-9A-Z]{9})/i)?.[1] || `html-${pageNumber}-${index}`;
    imageItems.push({
      asin,
      brand: brandHeading,
      title,
      size: extractSize(title),
      url: asin.startsWith("B") ? `https://www.amazon.com/dp/${asin}` : null,
      price: `$${priceMatch[1]}`,
      rating: `${ratingMatch[1]} out of 5 stars`,
      reviewCount: extractReviewCount(segment, ratingMatch),
      badge: null,
      image: imageUrl,
      sponsored: false,
      origin: null,
      acidity: null,
    });
  }
  if (imageItems.length) return imageItems;

  const lines = markdown
    .split(/\r?\n/)
    .map(clean)
    .filter(Boolean);
  const out = [];

  for (let i = 0; i < lines.length; i += 1) {
    const ratingMatch = lines[i].match(/^([0-5](?:\.\d)?)\s+out of 5 stars\.?$/i);
    if (!ratingMatch) continue;

    const candidates = [];
    let sponsoredNearby = false;
    for (let j = i - 1; j >= Math.max(0, i - 7); j -= 1) {
      const value = lines[j];
      if (/^sponsored\b/i.test(value)) {
        sponsoredNearby = true;
        continue;
      }
      if (/^\d(?:\.\d)?$|^\([\d.,]+[KM]?\)$|^Image$/i.test(value)) continue;
      if (/^\$|out of 5 stars/i.test(value)) continue;
      if (value.length >= 12 && looksLikeTitle(value)) candidates.push(value);
    }
    if (sponsoredNearby) continue;
    const title = candidates.sort((a, b) => b.length - a.length)[0];
    if (!title || !isRelevantProduct(title)) continue;

    let price = null;
    let reviewCount = null;
    for (let j = i + 1; j <= Math.min(lines.length - 1, i + 12); j += 1) {
      if (!reviewCount) reviewCount = lines[j].match(/^\(([\d.,]+[KM]?)\)$/)?.[1] || null;
      const priceMatch = lines[j].match(/^\$([\d,]+(?:\.\d{2})?)/);
      if (priceMatch) {
        price = `$${priceMatch[1]}`;
        break;
      }
    }
    if (!price) continue;

    out.push({
      asin: `html-${pageNumber}-${i}`,
      brand: null,
      title,
      size: extractSize(title),
      url: null,
      price,
      rating: `${ratingMatch[1]} out of 5 stars`,
      reviewCount,
      badge: null,
      image: null,
      sponsored: false,
      origin: null,
      acidity: null,
    });
  }
  return out;
}

/** Amazon 공개 검색 HTML을 텍스트로 받아 파싱한다. Playwright는 사용하지 않는다. */
export async function scrapeAmazon(
  _page,
  query,
  limit = 5,
  sort = "best-sellers",
  department = "grocery",
  maxPages = 2
) {
  const sParam = SORT_MAP[sort] ?? SORT_MAP["best-sellers"];
  const seen = new Set();
  const collected = [];

  warnIfNoCookie();

  const fetchPage = async (sourceUrl) => {
    const response = await fetch(`https://r.jina.ai/${sourceUrl}`, {
      signal: AbortSignal.timeout(30000),
      headers: {
        Accept: "text/plain",
        "User-Agent": "amazon-aeon-enrich/1.0",
        "X-No-Cache": "true",
        ...(AMAZON_COOKIE ? { "X-Set-Cookie": AMAZON_COOKIE } : {}),
      },
    });
    if (!response.ok) throw new Error(`Amazon HTML 리더 응답 실패: HTTP ${response.status}`);
    return response.text();
  };

  // 반복되는 검색어 URL이 캐시/봇체크 스냅샷으로 응답되는 문제를 막기 위해 매 요청마다
  // 랜덤 타임스탬프 파라미터를 붙인다 (oliveoil-scraper v34에서 확립된 방식).
  const buildSourceUrl = (pageNumber) => {
    const params = new URLSearchParams({ k: query, page: String(pageNumber) });
    if (department && department !== "all") params.set("i", department);
    if (sParam) params.set("s", sParam);
    params.set("_t", `${Date.now()}${Math.floor(Math.random() * 1000)}`);
    return `http://www.amazon.com/s?${params.toString()}`;
  };

  for (let pageNumber = 1; pageNumber <= maxPages; pageNumber += 1) {
    const sourceUrl1 = buildSourceUrl(pageNumber);
    let markdown = await fetchPage(sourceUrl1);
    let items = parseAmazonMarkdown(markdown, pageNumber);

    for (let attempt = 1; items.length === 0 && pageNumber === 1 && attempt <= 3; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, attempt * 3000));
      markdown = await fetchPage(buildSourceUrl(pageNumber));
      items = parseAmazonMarkdown(markdown, pageNumber);
    }

    // 재시도까지 다 끝난 "최종" 마크다운을 저장한다 (재시도 전 첫 시도만 저장하면 로딩스피너
    // 스냅샷 같은 실패 상태만 남고, 정작 파싱에 성공한 진짜 내용은 못 보게 되는 문제가 있었음).
    if (pageNumber === 1) {
      try {
        fs.mkdirSync("output", { recursive: true });
        const debugFile = path.join("output", `debug-amazon-last-${query.replace(/[^a-z0-9]+/gi, "_").slice(0, 80)}.md`);
        fs.writeFileSync(debugFile, `<!-- 요청 URL: ${sourceUrl1} / 파싱된 상품 수: ${items.length} -->\n\n${markdown}`, "utf-8");
      } catch {
        // 디버그 저장 실패는 무시
      }
    }

    if (items.length === 0) {
      if (pageNumber === 1) {
        let debugPath = null;
        try {
          fs.mkdirSync("output", { recursive: true });
          debugPath = path.join("output", `debug-amazon-${query.replace(/[^a-z0-9]+/gi, "_").slice(0, 80)}.md`);
          fs.writeFileSync(debugPath, markdown, "utf-8");
        } catch {
          debugPath = null;
        }
        throw new Error(
          `Amazon 검색 HTML에서 상품을 찾지 못했습니다.${debugPath ? ` (원본 저장: ${debugPath})` : ""}`
        );
      }
      break;
    }

    let newCount = 0;
    for (const item of items) {
      const key = baseTitleKey(item.title) || item.title.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      newCount += 1;
      collected.push(item);
      if (collected.length >= limit) return collected.slice(0, limit);
    }
    console.log(`  [amazon] ${pageNumber}페이지: ${items.length}개, 누적 ${collected.length}개`);
    if (newCount === 0) break;
  }
  return collected;
}
