import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { scrapeAmazon } from "./scrapers/amazon.js";
import { scrapeAeon } from "./scrapers/aeon.js";
import { parseCsvObjects } from "./csv.js";

// 83개 상품유형 × (아마존/이온몰)을 순회하며 매달 베스트셀러 순위를 크롤링한다.
// D:\AI 프로젝트\유통사크롤러\amazon-aeon-enrich(로컬 전용, 검증됨)의 r.jina.ai 리더 프록시
// 스크래퍼(scrapers/amazon.js, aeon.js)를 그대로 재사용한다 — GitHub Actions에서 돌리려면
// 리포 안에 있어야 해서 이식했다.

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function parseArgs(argv) {
  const args = { site: "all", limit: 40, only: "", results: "results.jsonl", maxPages: 2 };
  for (const raw of argv) {
    const m = raw.match(/^--([a-zA-Z]+)=(.*)$/);
    if (!m) continue;
    const [, key, value] = m;
    if (key === "limit" || key === "maxPages") args[key] = Number(value);
    else args[key] = value;
  }
  return args;
}

function loadTypeQueryMap() {
  const csvPath = path.join(__dirname, "type-query-map.csv");
  const text = fs.readFileSync(csvPath, "utf-8");
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
  const sites = args.site === "all" ? ["amazon", "aeon"] : [args.site];
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
    `[crawl] 대상 ${rows.length}개 유형 × ${sites.length}개 사이트, limit=${args.limit}, results=${resultsPath}`
  );

  let ok = 0;
  let failed = 0;
  let skipped = 0;

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
        if (site === "amazon") {
          items = await scrapeAmazon(null, query, args.limit, "best-sellers", "grocery", args.maxPages);
        } else {
          items = await scrapeAeon(query, args.limit, args.maxPages);
        }
        appendResult(resultsPath, {
          productType,
          category,
          query,
          retailer: site,
          status: "ok",
          items,
        });
        ok += 1;
        console.log(`[crawl] OK ${site} "${productType}" (${query}) → ${items.length}개`);
      } catch (error) {
        appendResult(resultsPath, {
          productType,
          category,
          query,
          retailer: site,
          status: "error",
          error: error.message,
        });
        failed += 1;
        console.warn(`[crawl] 실패 ${site} "${productType}" (${query}): ${error.message}`);
      }

      // 요청 간 짧은 지연 — 반복 호출로 봇체크 걸릴 확률을 줄인다.
      await sleep(1500 + Math.random() * 1000);
    }
  }

  console.log(`[crawl] 완료: 성공 ${ok}건, 실패 ${failed}건, 스킵(이미 완료) ${skipped}건`);
}

main().catch((error) => {
  console.error("[crawl] 치명적 오류:", error);
  process.exit(1);
});
