import { waitForHumanIfChallenged } from "./human-check.js";

const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

// 월마트 검색 정렬 옵션 → URL의 sort= 파라미터 매핑
const SORT_MAP = {
  featured: "best_match",
  "best-sellers": "best_seller",
  "price-asc": "price_low",
  "price-desc": "price_high",
  reviews: "rating_high",
  newest: "new",
};

const isChallenged = (text) =>
  /Robot or human|Verify you are a human|Press (&|and) Hold|are you a robot/i.test(text || "");

/**
 * 월마트 검색 결과에서 상위 limit개 "오가닉(광고 제외)" 제품정보를 긁어온다.
 * sort: "featured" | "best-sellers" | "price-asc" | "price-desc" | "reviews" | "newest"
 * interactive: true(=headed 모드)면 봇 확인 화면에서 페이지를 새로고침하지 않고 사람이
 *   직접 버튼을 누를 때까지 같은 화면에서 기다린다.
 * DOM 구조는 월마트가 수시로 바꾸므로, 실패 시 selector 조정이 필요할 수 있다.
 */
export async function scrapeWalmart(page, query, limit = 5, sort = "best-sellers", interactive = false) {
  const sortParam = SORT_MAP[sort] ?? SORT_MAP["best-sellers"];
  const url = `https://www.walmart.com/search?q=${encodeURIComponent(query)}&sort=${sortParam}`;
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
  // 챌린지 스크립트가 렌더링될 시간을 조금 준다.
  await page.waitForTimeout(2000);

  const passed = await waitForHumanIfChallenged(
    page,
    async () => isChallenged(await page.textContent("body").catch(() => "")),
    "walmart",
    { interactive }
  );
  if (!passed) {
    throw new Error(
      "월마트 로봇 확인(캡차) 페이지가 떴습니다. --headed 옵션으로 직접 풀거나, 잠시 후 다시 시도하세요."
    );
  }

  await page.waitForSelector("[data-item-id]", { timeout: 20000 }).catch(() => {});

  const rawItems = await page.$$eval("[data-item-id]", (nodes) => {
    const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

    const parseSize = (innerText, title) => {
      const combined = `${title} ${innerText}`;
      const m = combined.match(
        /(\d+(?:\.\d+)?\s?(?:fl\.?\s?oz|fluid ounces?|ml|milliliters?|\bL\b|liters?|litres?|qt|gal))/i
      );
      return m ? clean(m[0]) : null;
    };

    // 카테고리별 불용어 목록(예: 올리브유 전용 Extra/Virgin/Olive/Oil...)은 83개 유형
    // 전체로 확장하면서 다른 품목에서 오작동해 폐기함. 월마트 상품명은 콤마가 브랜드가
    // 아니라 상품명 맨 끝(용량 표기 앞)에 오는 경우가 많아 콤마를 경계 신호로 못 쓰고,
    // 메인페이지 기존 데이터 실측 검증(50% 샘플) 기준 가장 무난했던 "앞 2단어 고정"만 쓴다
    // (scripts/crawl-product-sourcing/brand-extract.js의 extractBrandFixedWords와 동일 로직 —
    // 이 함수는 page.$$eval 안에서 브라우저 컨텍스트로 직렬화돼 실행되므로 import 불가해 인라인함).
    const parseBrand = (title) => {
      // 맨 앞 수량/묶음 표기("2X-", "2x ", "1 x ")는 브랜드가 아니므로 제거 후 자름
      // (brand-extract.js stripLeadingQty와 동일 로직, 인라인 이유는 위 주석 참고).
      const clean = String(title || "").replace(/^\d+\s*[xX]\s*-?\s*/, "");
      const words = clean.split(/\s+/).filter(Boolean);
      return words.slice(0, 2).join(" ") || null;
    };

    const out = [];
    for (const node of nodes) {
      const itemId = node.getAttribute("data-item-id");
      if (!itemId) continue;

      const titleEl = node.querySelector('span[data-automation-id="product-title"]');
      const title = clean(titleEl?.textContent);
      if (!title) continue;

      const linkEl = node.querySelector("a");
      const href = linkEl?.getAttribute("href");
      const productUrl = href
        ? href.startsWith("http")
          ? href.split("?")[0]
          : "https://www.walmart.com" + href.split("?")[0]
        : null;

      const priceEl = node.querySelector(
        '[data-automation-id="product-price"] span[aria-hidden="true"], [data-automation-id="product-price"]'
      );
      const price = clean(priceEl?.textContent);

      const ratingEl = node.querySelector(
        '[data-testid="product-ratings"], span[aria-label*="stars"], span[aria-label*="out of"]'
      );
      const rating = clean(ratingEl?.getAttribute("aria-label") || ratingEl?.textContent);

      const reviewEl = node.querySelector(
        '[data-testid="product-reviews-count"], [link-identifier="linkText"]'
      );
      const reviewCount = clean(reviewEl?.textContent);

      const badgeEl = node.querySelector('[data-testid="badge-text"]');
      const badge = clean(badgeEl?.textContent); // "Best seller", "Rollback" 등

      const imgEl = node.querySelector("img");
      const image = imgEl?.getAttribute("src") || null;

      const innerText = clean(node.innerText || "");
      const sponsored = /^Sponsored\b/i.test(innerText) || /\bSponsored\b/.test(
        clean(node.querySelector('[data-testid="sponsoredLabel"]')?.textContent || "")
      );

      const size = parseSize(innerText, title);
      const brand = parseBrand(title);

      out.push({
        itemId,
        brand,
        title,
        size,
        url: productUrl,
        price: price || null,
        rating: rating || null,
        reviewCount: reviewCount || null,
        badge: badge || null,
        image,
        sponsored,
        origin: null, // enrich 단계에서 채움
        acidity: null, // enrich 단계에서 채움
      });
    }
    return out;
  });

  // 광고 제외 + itemId 중복 제거 후 상위 limit개만 반환
  const seen = new Set();
  const organic = [];
  for (const item of rawItems) {
    if (item.sponsored) continue;
    if (seen.has(item.itemId)) continue;
    seen.add(item.itemId);
    organic.push(item);
    if (organic.length >= limit) break;
  }

  return organic;
}

/**
 * 상품 상세페이지를 방문해서 갤러리 사진(정면/후면/측면 등) URL을 여러 장 뽑는다.
 * 원산지판독(verify_origin_gemini.py)이 후면 라벨 사진을 볼 수 있게 하려는 용도 —
 * 검색결과 카드에는 정면 썸네일 1장만 있어서 이게 필요하다.
 *
 * ⚠️ 실제 월마트 상세페이지 마크업으로 검증 안 됨(이 프로젝트 환경은 브라우저 접근이
 * 없어 실행 자체를 못 함) — 후보 selector를 여러 개 순서대로 시도하는 방어적 구현.
 * 실행해보고 0장만 나오면 실제 DOM 보면서 selector 보강 필요.
 *
 * detail 페이지 방문도 search와 마찬가지로 봇 확인(캡차)이 뜰 수 있어서 동일하게
 * waitForHumanIfChallenged로 처리한다 — 호출 쪽(crawl.js)에서 이미 캡차를 한 번
 * 통과했다면 같은 브라우저 컨텍스트라 세션이 유지돼 대부분 안 뜰 것으로 기대되지만,
 * 상세페이지마다 새로 뜰 가능성도 배제 못 함(그래서 호출 횟수를 제한해서 쓸 것 —
 * crawl.js의 --gallery-limit 참고).
 */
export async function scrapeWalmartProductImages(page, productUrl, maxImages = 4) {
  await page.goto(productUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(1500);

  const passed = await waitForHumanIfChallenged(
    page,
    async () => isChallenged(await page.textContent("body").catch(() => "")),
    "walmart-detail",
    { interactive: true, maxWaitMs: 60000 }
  );
  if (!passed) return [];

  await page.waitForTimeout(500);

  const images = await page.evaluate((max) => {
    const candidateSelectors = [
      '[data-testid="media-thumbnail"] img',
      '[data-testid="hero-carousel"] img',
      '[data-testid="ProductImageCarousel"] img',
      '[class*="thumbnail" i] img',
      '[class*="carousel" i] img',
      'img[data-testid="productImage"]',
    ];
    const seen = new Set();
    const out = [];
    for (const sel of candidateSelectors) {
      const nodes = document.querySelectorAll(sel);
      for (const img of nodes) {
        const src = img.getAttribute("src") || img.getAttribute("data-src");
        if (!src || seen.has(src)) continue;
        seen.add(src);
        out.push(src);
        if (out.length >= max) return out;
      }
      if (out.length >= max) break;
    }
    return out;
  }, maxImages);

  return images;
}
