import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { scrapeWalmart, scrapeWalmartProductImages } from "./scrapers/walmart.js";
import { scrapeSamsClub, scrapeSamsClubProductImages } from "./scrapers/samsclub.js";
import { parseCsvObjects } from "../crawl-product-sourcing/csv.js";

// 83개 상품유형 × (월마트/샘스클럽)을 순회하며 베스트셀러 순위를 크롤링한다.
// oliveoil-scraper의 scrapers/walmart.js, samsclub.js, human-check.js를 그대로 이식했다 —
// PerimeterX "Press & Hold" 봇차단 때문에 완전 무인은 불가능해서, headed 브라우저를 하나만
// 띄워 전체 루프 동안 재사용한다(캡차를 한 번 통과하면 그 세션 쿠키가 이후 요청에도 남아있길
// 기대하는 설계 — 매번 새 브라우저를 띄우면 매번 캡차가 뜰 가능성이 커진다). GitHub Actions
// 워크플로가 이 headed 브라우저 화면을 VNC로 사람에게 보여주고, 캡차가 뜰 때마다
// human-check.js(waitForHumanIfChallenged, 최대 90초 대기)가 사람이 눌러줄 때까지 기다린다.

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const TYPE_QUERY_MAP_PATH = path.join(__dirname, "..", "crawl-product-sourcing", "type-query-map.csv");

function parseArgs(argv) {
  const args = { site: "all", limit: 40, only: "", results: "results.jsonl", galleryLimit: 0 };
  for (const raw of argv) {
    const m = raw.match(/^--([a-zA-Z-]+)=(.*)$/);
    if (!m) continue;
    const [, rawKey, value] = m;
    const key = rawKey === "gallery-limit" ? "galleryLimit" : rawKey;
    if (key === "limit" || key === "galleryLimit") args[key] = Number(value);
    else args[key] = value;
  }
  return args;
}

function loadTypeQueryMap() {
  const text = fs.readFileSync(TYPE_QUERY_MAP_PATH, "utf-8");
  return parseCsvObjects(text).filter((row) => row.product_type);
}

function loadDoneKeys(resultsPath) {
  const done = new Set();
  if (!fs.existsSync(resultsPath)) return done;
  const lines = fs.readFileSync(resultsPath, "utf-8").split("\n").filter(Boolean);
  for (const line of lines) {
    try {
      const record = JSON.parse(line);
      done.add(`${record.productType}|${record.retailer}`);
    } catch {
      // 손상된 줄은 무시 (재실행 시 다시 크롤링됨)
    }
  }
  return done;
}

function appendResult(resultsPath, record) {
  fs.appendFileSync(resultsPath, `${JSON.stringify(record)}\n`, "utf-8");
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const sites = args.site === "all" ? ["walmart", "samsclub"] : [args.site];
  const rows = loadTypeQueryMap().filter((row) => {
    if (!args.only) return true;
    const needle = args.only.toLowerCase();
    return (
      row.product_type.toLowerCase().includes(needle) ||
      (row["대분류"] || "").toLowerCase().includes(needle)
    );
  });

  const resultsPath = path.resolve(args.results);
  const done = loadDoneKeys(resultsPath);

  console.log(
    `[crawl] 대상 ${rows.length}개 유형 × ${sites.length}개 사이트, limit=${args.limit}, ` +
    `galleryLimit=${args.galleryLimit}, results=${resultsPath}`
  );
  if (args.galleryLimit > 0) {
    console.log(
      `[crawl] --gallery-limit=${args.galleryLimit} — 유형×사이트마다 상위 ${args.galleryLimit}개 ` +
      `상품은 상세페이지까지 방문해서 원산지판독용 갤러리 사진을 추가로 수집합니다 ` +
      `(페이지 방문이 늘어나 캡차가 더 자주 뜰 수 있고 실행 시간도 길어집니다).`
    );
  }

  console.log("[crawl] Chromium 실행 (headed, VNC로 사람이 볼 수 있음)...");
  const browser = await chromium.launch({ headless: false, args: ["--start-maximized"] });
  const context = await browser.newContext({ viewport: null });
  const page = await context.newPage();

  let ok = 0;
  let failed = 0;
  let skipped = 0;

  try {
    for (const row of rows) {
      const productType = row.product_type;
      const category = row["대분류"];
      const query = row.query;

      for (const site of sites) {
        const key = `${productType}|${site}`;
        if (done.has(key)) {
          skipped += 1;
          continue;
        }

        try {
          let items;
          if (site === "walmart") {
            items = await scrapeWalmart(page, query, args.limit, "best-sellers", true);
          } else {
            items = await scrapeSamsClub(page, query, args.limit, true);
          }

          if (args.galleryLimit > 0) {
            const targets = items.slice(0, args.galleryLimit).filter((it) => it.url);
            for (const item of targets) {
              try {
                const imageUrls = site === "walmart"
                  ? await scrapeWalmartProductImages(page, item.url)
                  : await scrapeSamsClubProductImages(page, item.url);
                if (imageUrls.length) {
                  item.imageUrls = imageUrls;
                  console.log(`[crawl]   갤러리 ${imageUrls.length}장 확보: ${item.title?.slice(0, 40)}`);
                }
              } catch (galleryError) {
                console.warn(`[crawl]   갤러리 수집 실패(무시하고 계속): ${galleryError.message}`);
              }
              await sleep(1200 + Math.random() * 800);
            }
          }

          appendResult(resultsPath, {
            productType, category, query, retailer: site, status: "ok", items,
          });
          ok += 1;
          console.log(`[crawl] OK ${site} "${productType}" (${query}) → ${items.length}개`);
        } catch (error) {
          appendResult(resultsPath, {
            productType, category, query, retailer: site, status: "error", error: error.message,
          });
          failed += 1;
          console.warn(`[crawl] 실패 ${site} "${productType}" (${query}): ${error.message}`);
        }

        await sleep(1500 + Math.random() * 1000);
      }
    }
  } finally {
    await browser.close();
  }

  console.log(`[crawl] 완료: 성공 ${ok}건, 실패 ${failed}건, 스킵(이미 완료) ${skipped}건`);
}

main().catch((error) => {
  console.error("[crawl] 치명적 오류:", error);
  process.exit(1);
});
