import { waitForHumanIfChallenged } from "./human-check.js";

const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

const isChallenged = (text) =>
  /Robot or human|Verify you are a human|Press (&|and) Hold|are you a robot|Access Denied|no robots allowed/i.test(
    text || ""
  );

/**
 * 샘스클럽 검색 결과에서 상위 limit개 제품정보를 긁어온다.
 * 정렬 UI를 클릭해서 "Best Selling"을 시도하고, 안 되면 기본(관련도) 정렬로 진행한다.
 * interactive: true(=headed 모드)면 봇 확인 화면에서 페이지를 새로고침하지 않고 사람이
 *   직접 버튼을 누를 때까지 같은 화면에서 기다린다.
 * 실제 마크업을 본 적이 없는 추측 기반 구현이라, 실행 결과를 보면서 selector를 맞춰야 한다.
 */
export async function scrapeSamsClub(page, query, limit = 5, interactive = false) {
  const url = `https://www.samsclub.com/s/${encodeURIComponent(query)}`;
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(2000);

  const passed = await waitForHumanIfChallenged(
    page,
    async () => isChallenged(await page.textContent("body").catch(() => "")),
    "samsclub",
    { interactive }
  );
  if (!passed) {
    throw new Error(
      "샘스클럽 로봇 확인 페이지가 떴습니다. --headed 옵션으로 직접 풀거나, 잠시 후 다시 시도하세요."
    );
  }

  // 정렬을 Best Selling으로 바꿔본다 (UI 클릭 방식 - URL 파라미터를 몰라도 동작)
  let sortApplied = false;
  try {
    const sortTrigger = page.getByRole("button", { name: /sort/i }).first();
    if (await sortTrigger.count()) {
      await sortTrigger.click({ timeout: 5000 });
      const option = page.getByText(/best.?sell(ing|ers)?|top.?sell(ing|ers)?/i).first();
      if (await option.count()) {
        await option.click({ timeout: 5000 });
        await page.waitForTimeout(2500);
        sortApplied = true;
      }
    }
  } catch {
    // 정렬 UI를 못 찾으면 기본(관련도) 정렬로 그냥 진행
  }
  if (!sortApplied) {
    console.warn(
      "[samsclub] Best Selling 정렬 UI를 못 찾음 — 기본(관련도) 정렬로 진행. selector 보강 필요할 수 있음."
    );
  }

  await page.waitForTimeout(1000);

  // 2026-08 기준으로 상품 카드가 <a href="/p/...">를 더 이상 안 쓰는 구조로 바뀌어서
  // (경로 자체도 /p/ → /ip/로 변경됨) DOM 앵커 기반 스크래핑이 항상 0건을 반환하게 됨
  // (실제 크롤링 로그로 확인: "OK samsclub ... → 0개"). Next.js가 페이지에 심어두는
  // __NEXT_DATA__(SSR 시 받은 검색 API 응답 원본)에서 직접 읽는 방식으로 교체 — 이 쪽은
  // 마크업/CSS 변경에 안 흔들리고 가격/평점/URL이 이미 구조화돼 있어 더 정확함.
  const rawItems = await page.evaluate((limit) => {
    const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

    function findProductItems(obj) {
      if (obj && typeof obj === "object") {
        if (Array.isArray(obj) && obj.length && obj[0]?.__typename === "Product") return obj;
        for (const k in obj) {
          const found = findProductItems(obj[k]);
          if (found) return found;
        }
      }
      return null;
    }

    const items = window.__NEXT_DATA__ ? findProductItems(window.__NEXT_DATA__.props) : null;
    if (!items) return [];

    const out = [];
    for (const it of items) {
      const title = clean(it.name);
      if (!title || title.length < 5 || !it.canonicalUrl) continue;

      const sizeMatch = title.match(
        /(\d+(?:\.\d+)?\s?(?:fl\.?\s?oz|fluid ounces?|ml|milliliters?|\bL\b|liters?|litres?|qt|gal|ct|count))/i
      );
      const size = sizeMatch ? clean(sizeMatch[0]) : null;

      // 카테고리별 불용어 목록은 83개 유형 전체로 확장하면서 다른 품목에서 오작동해
      // 폐기함(walmart.js와 같은 이유 — 상세 근거는 그쪽 주석 참고). 샘스클럽도 콤마가
      // 브랜드 경계가 아니라 상품명 끝 용량 표기 앞에 오는 경우가 많아 "앞 2단어 고정"만 쓴다.
      // 맨 앞 수량/묶음 표기("2X-" 등)는 브랜드가 아니므로 제거 후 자름.
      // API의 brand 필드는 대부분 null이라(실측) 우선순위 삼기 어려워 폴백으로만 씀.
      const titleForBrand = title.replace(/^\d+\s*[xX]\s*-?\s*/, "");
      const words = titleForBrand.split(/\s+/).filter(Boolean);
      const brand = it.brand || words.slice(0, 2).join(" ") || null;

      const price = it.priceInfo?.linePrice || it.priceInfo?.itemPrice || null;
      const rating = it.averageRating != null ? String(it.averageRating) : null;
      const reviewCount = it.numberOfReviews != null ? String(it.numberOfReviews) : null;
      const sponsored = it.sponsoredProduct != null;
      const image = it.imageInfo?.thumbnailUrl || null;

      out.push({
        id: it.usItemId || it.canonicalUrl,
        brand,
        title,
        size,
        url: it.canonicalUrl.startsWith("http")
          ? it.canonicalUrl
          : "https://www.samsclub.com" + it.canonicalUrl,
        price,
        rating,
        reviewCount,
        badge: null,
        image,
        sponsored,
        origin: null,
        acidity: null,
      });
      if (out.length >= limit * 3) break; // 광고 제외 위해 넉넉히 모음
    }
    return out;
  }, limit);

  const organic = rawItems.filter((it) => !it.sponsored).slice(0, limit);
  return organic.length ? organic : rawItems.slice(0, limit);
}

/**
 * 상품 상세페이지를 방문해서 갤러리 사진(정면/후면/측면 등) URL을 여러 장 뽑는다.
 * walmart.js의 scrapeWalmartProductImages와 같은 목적(원산지판독용 후면 라벨 사진 확보).
 *
 * 검색결과와 마찬가지로 DOM selector 대신 __NEXT_DATA__의
 * product.imageInfo.allImages(원본 갤러리 사진 URL 배열, 실측 확인함)를 우선 사용하고,
 * 혹시 그 경로가 또 바뀌는 경우를 대비해 DOM selector를 폴백으로 남겨둔다.
 */
export async function scrapeSamsClubProductImages(page, productUrl, maxImages = 4) {
  await page.goto(productUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(1500);

  const passed = await waitForHumanIfChallenged(
    page,
    async () => isChallenged(await page.textContent("body").catch(() => "")),
    "samsclub-detail",
    { interactive: true, maxWaitMs: 60000 }
  );
  if (!passed) return [];

  await page.waitForTimeout(500);

  const images = await page.evaluate((max) => {
    function findAllImages(obj) {
      if (obj && typeof obj === "object") {
        if (
          Array.isArray(obj.allImages) &&
          obj.allImages.length &&
          typeof obj.allImages[0]?.url === "string"
        ) {
          return obj.allImages;
        }
        for (const k in obj) {
          const found = findAllImages(obj[k]);
          if (found) return found;
        }
      }
      return null;
    }

    const allImages = window.__NEXT_DATA__ ? findAllImages(window.__NEXT_DATA__.props) : null;
    if (allImages) {
      return allImages.slice(0, max).map((img) => img.url);
    }

    // 폴백: JSON 경로가 또 바뀐 경우를 대비한 DOM selector 방어적 구현.
    const candidateSelectors = [
      '[class*="thumbnail" i] img',
      '[class*="carousel" i] img',
      '[class*="gallery" i] img',
      '[data-testid*="image" i] img',
      'button[class*="thumb" i] img',
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
