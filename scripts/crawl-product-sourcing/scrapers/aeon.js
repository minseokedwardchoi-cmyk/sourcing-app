import fs from "node:fs";
import path from "node:path";
import { translateToJapanese } from "../translate.js";
import { extractBrandCommaOrWords } from "../brand-extract.js";

// oliveoil-scraper의 scrapers/aeon.js를 거의 그대로 가져오되, isQueryRelevant()(올리브유류
// 전용 "family" 체크가 섞여 있던 함수)는 제거했다 — 이번 작업은 올리브유 한정이 아니라
// 임의의 83개 상품유형을 대상으로 하므로, 관련성 판단은 이후 match.js의 브랜드/용량 검증에
// 맡기고 여기서는 명백히 무관한 카테고리(GENERIC_EXCLUDED)만 걸러낸다.

const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();

// myaeon2go 이미지는 thumbor 리사이징 프록시(thumbor.../unsafe/trim/fit-in/.../filters:.../
// https://catalog-assets-.../<base64 경로 조각들>.jpg)를 거치는데, 이 프록시가 사용자 실측으로
// HTTP 504(응답 지연)를 자주 낸다고 확인됨. base64 경로 조각들을 디코딩하면
// storage.googleapis.com/... 같은 실제 원본 경로가 나온다 — 프록시를 거치지 않는 이 원본
// URL로 바꿔서 저장한다. ⚠️ 디코딩된 URL이 실제로 항상 접근 가능한지는 이 환경에서 네트워크
// 호출로 검증하지 못했다 — 로컬에서 실제로 열리는지 확인 필요. 디코딩 실패/형태가 안 맞으면
// 원본 thumbor URL을 그대로 반환(안전한 폴백).
export function decodeThumborOriginUrl(imageUrl) {
  if (!imageUrl) return imageUrl;
  try {
    // thumbor는 "filters:.../<원본 URL 그대로>" 형태로 원본을 감싼다. 원본 호스트가
    // catalog-assets류면 그 경로 자체가 base64 조각들이라 한 번 더 디코딩해야 진짜 원본이
    // 나오고(아래), assets.myboxed.com.my처럼 이미 평문 경로인 호스트도 실측으로 확인됨 —
    // 그 경우는 thumbor 감싸는 부분만 벗겨내면 바로 원본 URL이다.
    const allMatches = Array.from(imageUrl.matchAll(/https?:\/\//gi));
    if (!allMatches.length) return imageUrl;
    const embeddedUrl = imageUrl.slice(allMatches[allMatches.length - 1].index);

    const catalogMatch = embeddedUrl.match(/https?:\/\/[^/]*catalog-assets[^/]+\/(.+)$/i);
    if (!catalogMatch) return embeddedUrl; // catalog-assets가 아니면 이미 평문 원본 URL이므로 그대로 사용

    let tail = catalogMatch[1];
    const extMatch = tail.match(/\.(jpe?g|png|webp)$/i);
    if (!extMatch) return embeddedUrl;
    const ext = extMatch[0];
    tail = tail.slice(0, -ext.length);
    const segments = tail.split("/").filter(Boolean);
    const decoded = segments.map((seg) => {
      const padded = seg + "=".repeat((4 - (seg.length % 4)) % 4);
      return Buffer.from(padded, "base64").toString("utf-8");
    });
    if (!decoded.length || !/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(decoded[0])) return embeddedUrl;
    return `https://${decoded.join("/")}${ext}`;
  } catch {
    return imageUrl;
  }
}

const GENERIC_EXCLUDED = [
  "shampoo", "conditioner", "serum", "moisturizer", "cologne", "perfume",
  "hair oil", "skin oil", "body oil", "cleansing oil", "lotion", "detergent",
  "air freshener", "candle", "soap",
  "シャンプー", "リンス", "洗剤", "石鹸", "石けん", "化粧", "香水", "柔軟剤", "ペット",
  "ログイン", "ログアウト", "会員登録", "新規登録", "カートに入れる", "マイページ",
  "お届け先", "サインイン",
];

const isRelevantProduct = (title) => {
  const text = clean(title).toLowerCase();
  return text && !GENERIC_EXCLUDED.some((word) => text.includes(word));
};

function extractSize(text) {
  return clean(text).match(/\d+(?:\.\d+)?\s?(?:g|kg|ml|l|liter|litre|oz)\b/i)?.[0] || null;
}

function stripMarkdown(value) {
  return clean(
    String(value || "")
      .replace(/!\[[^\]]*\]\([^)]+\)/g, " ")
      .replace(/<\/?\d+>/g, " ")
      .replace(/[*_`#]/g, " ")
  );
}

async function scrapeAeonMalaysia(query, limit = Infinity) {
  // 상품명에 "%"가 들어있으면(예: "Colavita 100% Premium...") myaeon2go 서버가 이걸 되처리하다가
  // 깨지는지 400 Bad Request를 내는 게 실측 확인됨(우리 쪽 encodeURIComponent 자체는 정상). 검색
  // 목적상 의미 손실이 거의 없으니 "%"만 지우고 보낸다.
  const safeQuery = query.replace(/%/g, "");
  const buildReaderUrl = () => {
    const sourceUrl = `http://myaeon2go.com/products/search/${encodeURIComponent(safeQuery)}?_t=${Date.now()}${Math.floor(Math.random() * 1000)}`;
    return `https://r.jina.ai/${sourceUrl}`;
  };

  const fetchPage = async () => {
    const response = await fetch(buildReaderUrl(), {
      signal: AbortSignal.timeout(30000),
      headers: { Accept: "text/plain", "User-Agent": "amazon-aeon-enrich/1.0", "X-No-Cache": "true" },
    });
    if (!response.ok) throw new Error(`myaeon2go HTML 리더 응답 실패: HTTP ${response.status}`);
    const text = await response.text();
    try {
      fs.mkdirSync("output", { recursive: true });
      const debugFile = path.join("output", `debug-aeonmy-last-${query.replace(/[^\p{L}\p{N}]+/gu, "_").slice(0, 60)}.md`);
      fs.writeFileSync(debugFile, text, "utf-8");
    } catch {
      // 디버그 저장 실패는 무시
    }
    return text;
  };

  let markdown = await fetchPage();
  let resultsStart = markdown.indexOf("Search Results");
  if (resultsStart < 0) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    markdown = await fetchPage();
    resultsStart = markdown.indexOf("Search Results");
  }
  if (resultsStart < 0) {
    throw new Error("myaeon2go 검색 결과 영역을 찾지 못했습니다. 검색 결과가 없거나 페이지 구조가 바뀌었습니다.");
  }
  const resultsText = markdown.slice(resultsStart, markdown.indexOf("## Featured Categories", resultsStart) || undefined);

  const byUrl = new Map();
  const productLinkPattern = /\*\s+\[([\s\S]*?)\]\((https?:\/\/myaeon2go\.com\/product\/[^)]+)\)/g;
  const productMatches = Array.from(resultsText.matchAll(productLinkPattern));
  for (let index = 0; index < productMatches.length; index += 1) {
    const match = productMatches[index];
    const content = match[1];
    const url = match[2];
    const titleMatch = content.match(/###\s*([\s\S]*?)(?=\s+(?:Discounted price of\s+)?RM\s*[\d,.]+)/i);
    const priceMatch = content.match(/(?:Discounted price of\s+)?RM\s*([\d,.]+)/i);
    if (!titleMatch || !priceMatch) continue;

    const title = stripMarkdown(titleMatch[1]);
    if (!isRelevantProduct(title)) continue;
    // ⚠️ myaeon2go 이미지 URL 자체에 thumbor 필터 문법으로 괄호가 들어있다(예:
    // "filters:quality(100):max_bytes(50000):upscale():fill(white)/...") — 그냥 "다음 )까지"
    // 잡는 정규식([^)]+)을 쓰면 URL 내부의 첫 괄호에서 멈춰서 URL이 중간에 잘리는 사고가 실측
    // 확인됨(잘린 URL로 나중에 decodeThumborOriginUrl도, 실제 이미지 로딩도 다 실패했음). 한 단계
    // 괄호 중첩까지는 허용하는 패턴으로 완전한 URL 끝까지 잡는다.
    const rawImage = content.match(/!\[[^\]]*\]\((https?:\/\/(?:[^()]|\([^()]*\))*)\)/)?.[1] || null;
    const image = decodeThumborOriginUrl(rawImage);
    const id = url.match(/\/product\/(\d+)/)?.[1] || url;
    // "좋아요 수"는 상품 링크 캡처 그룹 바깥, "ADD" 버튼 직후에 있다(실측 확인: "...ADD 9",
    // "...ADD 205" 등 — 다른 라벨 없이 숫자만 붙어 있음). 다음 상품 항목 시작 전까지 구간에서 찾는다.
    const segStart = match.index + match[0].length;
    const segEnd = productMatches[index + 1]?.index ?? resultsText.length;
    const trailingSegment = resultsText.slice(segStart, Math.min(segEnd, segStart + 500));
    const likeMatch = trailingSegment.match(/\bADD\s+(\d+)\b/);
    const likeCount = likeMatch ? Number(likeMatch[1]) : null;
    const candidate = {
      id,
      brand: extractBrandCommaOrWords(title),
      title,
      size: extractSize(title),
      url,
      price: `RM ${priceMatch[1]}`,
      rating: null,
      reviewCount: null,
      likeCount,
      badge: null,
      image,
      sponsored: false,
      origin: null,
      acidity: null,
      source: "aeon-my",
    };
    // 같은 상품 URL이 검색결과 페이지 안에 두 번(메인 목록 + 다른 섹션 등) 등장하는 경우가
    // 있는데, "더 긴 제목을 우선"하는 예전 규칙이 부작용을 낸 게 실측 확인됨 — 근처 가격 텍스트가
    // 잘못 딸려 들어와서 오히려 더 길고 지저분한 제목("... RM 27.99 to RM 53.99 ...")이 채택되고
    // 깨끗한 제목이 버려짐. 제목 안에 "RM 숫자"(가격) 패턴이 섞여 있으면 오염된 것으로 보고
    // 우선순위를 낮춘다 — 둘 다 깨끗하거나 둘 다 오염됐을 때만 길이로 비교.
    const looksContaminated = (t) => /\bRM\s*[\d,.]/i.test(t);
    const existing = byUrl.get(url);
    if (!existing) {
      byUrl.set(url, candidate);
    } else {
      const existingBad = looksContaminated(existing.title);
      const candidateBad = looksContaminated(title);
      if (existingBad && !candidateBad) byUrl.set(url, candidate);
      else if (existingBad === candidateBad && title.length > existing.title.length) byUrl.set(url, candidate);
    }
  }

  // ⚠️ 예전엔 여기서 dedupeByBaseTitle()(용량 표기를 지운 이름 기준 중복제거)까지 한 번 더
  // 돌렸는데, 이러면 "Filippo Berio Extra Virgin Olive Oil 250 ml"와 "...1 l"이 같은 키로
  // 취급돼서 먼저 나온 사이즈만 남고 나머지 사이즈는 후보 목록에서 아예 사라지는 사고가 실측으로
  // 확인됨(용량이 정답 매칭 기준인데, 그 용량 차이를 없애버리는 중복제거를 쓰면 안 됨). byUrl
  // 자체가 이미 URL 기준으로 중복 제거된 상태라 이걸로 충분하다.
  const collected = Array.from(byUrl.values()).slice(0, limit);
  console.log(`  [aeon-my] 검색 HTML에서 관련 상품 ${collected.length}개 수집`);
  return collected;
}

const JP_STORE_ID = "01050000006350";

function parseAeonJapanMarkdown(markdown) {
  const priceRe = /(?:¥\s?([\d,]+)|([\d,]+)\s?円)/;
  const out = [];
  const seen = new Set();

  const pushCandidate = (title, priceDigits, url, image) => {
    title = stripMarkdown(title);
    if (!title || title.length < 4) return;
    if (!isRelevantProduct(title)) return;
    const priceNum = Number(String(priceDigits).replace(/,/g, ""));
    if (!Number.isFinite(priceNum) || priceNum <= 0) return;
    const key = `${title}|${priceDigits}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({
      id: url || key,
      // 일본어 제목도 공백으로 브랜드/설명이 분리돼 있는 경우가 많아 같은 규칙을 적용한다
      // (완벽하진 않음 — 한자/가타카나 텍스트라 콤마 경계 자체가 잘 안 나올 수 있음).
      brand: extractBrandCommaOrWords(title),
      title,
      size: extractSize(title),
      url: url || null,
      price: `¥${priceDigits}`,
      rating: null,
      reviewCount: null,
      badge: null,
      image: image || null,
      sponsored: false,
      origin: null,
      acidity: null,
      source: "aeon-jp",
    });
  };

  // 말레이시아 쪽에서 URL 안 괄호 때문에 잘리는 사고가 실측 확인돼서([^)]+ → 첫 괄호에서 멈춤),
  // 지금까지 확인된 일본 이미지 URL(aeonnetshop.com/img/goods/.../JAN코드.jpg)엔 괄호가 없어서
  // 문제된 적은 없지만 예방 차원에서 같은 방식(괄호 한 단계 중첩 허용)으로 맞춰둔다.
  const imagePattern = /!\[([^\]]*)\]\((https?:\/\/(?:[^()]|\([^()]*\))*)\)/g;
  const imageMatches = Array.from(markdown.matchAll(imagePattern));
  for (let index = 0; index < imageMatches.length; index += 1) {
    const match = imageMatches[index];
    const imageUrl = match[2];
    const start = match.index + match[0].length;
    const nextStart = imageMatches[index + 1]?.index ?? markdown.length;
    const segment = markdown.slice(start, Math.min(nextStart, start + 4000));
    const priceMatch = segment.match(priceRe);
    if (!priceMatch) continue;
    const priceDigits = priceMatch[1] || priceMatch[2];
    const linkMatch = segment.match(/\[([^\]]{4,200})\]\((https?:\/\/shop\.aeon\.com\/[^)]+)\)/);
    const altTitle = clean(match[1]);
    const title = linkMatch ? linkMatch[1] : altTitle;
    if (!title) continue;
    pushCandidate(title, priceDigits, linkMatch?.[2] || null, imageUrl);
  }
  if (out.length) return out;

  const lines = markdown.split(/\r?\n/).map(clean).filter(Boolean);
  for (let i = 0; i < lines.length; i += 1) {
    const priceMatch = lines[i].match(priceRe);
    if (!priceMatch) continue;
    const priceDigits = priceMatch[1] || priceMatch[2];
    let title = null;
    let url = null;
    for (let j = i - 1; j >= Math.max(0, i - 6); j -= 1) {
      const value = lines[j];
      if (priceRe.test(value)) continue;
      if (/^!\[/.test(value)) continue;
      const linkMatch = value.match(/\[([^\]]{4,200})\]\((https?:\/\/shop\.aeon\.com\/[^)]+)\)/);
      const candidate = linkMatch ? linkMatch[1] : value;
      if (candidate.length >= 6) {
        title = candidate;
        url = linkMatch?.[2] || null;
        break;
      }
    }
    if (!title) continue;
    pushCandidate(title, priceDigits, url, null);
  }
  return out;
}

async function fetchAeonJapanPage(query, pageNumber) {
  const params = new URLSearchParams({ q: query });
  if (pageNumber > 1) params.set("p", String(pageNumber));
  params.set("_t", `${Date.now()}${Math.floor(Math.random() * 1000)}`);
  const sourceUrl = `https://shop.aeon.com/netsuper/${JP_STORE_ID}/search/?${params.toString()}`;
  const response = await fetch(`https://r.jina.ai/${sourceUrl}`, {
    signal: AbortSignal.timeout(30000),
    headers: { Accept: "text/plain", "User-Agent": "amazon-aeon-enrich/1.0", "X-No-Cache": "true" },
  });
  if (!response.ok) throw new Error(`이온몰(일본) HTML 리더 응답 실패: HTTP ${response.status}`);
  const text = await response.text();
  try {
    fs.mkdirSync("output", { recursive: true });
    const debugFile = path.join("output", `debug-aeonjp-last-${query.replace(/[^\p{L}\p{N}]+/gu, "_").slice(0, 60)}.md`);
    fs.writeFileSync(debugFile, `<!-- 요청 URL: ${sourceUrl} -->\n\n${text}`, "utf-8");
  } catch {
    // 디버그 저장 실패는 무시
  }
  return text;
}

async function scrapeAeonJapan(query, limit = Infinity, maxPages = 1) {
  const byKey = new Map();
  for (let pageNumber = 1; pageNumber <= maxPages; pageNumber += 1) {
    let markdown = await fetchAeonJapanPage(query, pageNumber);
    let items = parseAeonJapanMarkdown(markdown);
    if (items.length === 0 && pageNumber === 1) {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      markdown = await fetchAeonJapanPage(query, pageNumber);
      items = parseAeonJapanMarkdown(markdown);
    }
    if (items.length === 0) break;

    let newCount = 0;
    for (const item of items) {
      // ⚠️ baseTitleKey(용량 표기 제거)로 중복제거하면 같은 상품명의 다른 용량(예: 200g/500g)이
      // 같은 키로 취급돼 먼저 나온 것만 남고 나머지가 통째로 사라지는 사고가 말레이시아 쪽에서
      // 실측 확인됨 — 여기서도 같은 함수를 썼었어서 동일하게 고침. URL(또는 없으면 원문 제목
      // 그대로)을 키로 써서 용량이 다른 후보는 계속 별도로 남긴다.
      const key = item.url || item.title;
      if (key && byKey.has(key)) continue;
      byKey.set(key, item);
      newCount += 1;
    }
    console.log(`  [aeon-jp] ${pageNumber}페이지: ${items.length}개, 누적 ${byKey.size}개`);
    if (newCount === 0) break;
  }
  return Array.from(byKey.values()).slice(0, limit);
}

/**
 * 일본(shop.aeon.com) + 말레이시아(myaeon2go.com) 이온몰 결과를 합쳐서 반환한다.
 * query는 상품 원어 상품명을 그대로 넘기면 된다 — 이미 일본어면 번역이 사실상 원문을 그대로
 * 돌려주고, 영어/한글이면 일본어로 번역해서 일본 사이트에, 원문 그대로 말레이시아 사이트에 쓴다.
 */
export async function scrapeAeon(query, limit = Infinity, maxPages = 1, { skipJapan = false, skipMalaysia = false } = {}) {
  const results = [];

  if (!skipJapan) {
    try {
      const jaQuery = (await translateToJapanese(query)) || query;
      const jpItems = await scrapeAeonJapan(jaQuery, limit, maxPages);
      results.push(...jpItems);
    } catch (error) {
      console.warn(`  [aeon-jp] 실패: ${error.message}`);
    }
  }

  if (!skipMalaysia) {
    try {
      const myItems = await scrapeAeonMalaysia(query, limit);
      results.push(...myItems);
    } catch (error) {
      console.warn(`  [aeon-my] 실패: ${error.message}`);
    }
  }

  return results.slice(0, limit);
}
