/** 아주 단순한 RFC4180 CSV 파서 (따옴표로 감싼 필드 안의 쉼표/줄바꿈 지원). 외부 의존성 없이 사용하려고 직접 작성. */
export function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let inQuotes = false;
  const pushField = () => {
    row.push(field);
    field = "";
  };
  const pushRow = () => {
    pushField();
    rows.push(row);
    row = [];
  };
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 1;
        } else {
          inQuotes = false;
        }
      } else {
        field += ch;
      }
      continue;
    }
    if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
      pushField();
    } else if (ch === "\n") {
      if (field.endsWith("\r")) field = field.slice(0, -1);
      pushRow();
    } else {
      field += ch;
    }
  }
  if (field.length || row.length) pushRow();
  return rows.filter((r) => r.length > 1 || r[0] !== "");
}

/** 헤더 행을 컬럼명으로 쓰는 객체 배열로 파싱한다. */
export function parseCsvObjects(text) {
  const rows = parseCsv(text.replace(/^﻿/, ""));
  if (!rows.length) return [];
  const header = rows[0];
  return rows.slice(1).map((r) => {
    const obj = {};
    header.forEach((key, idx) => {
      obj[key] = r[idx] ?? "";
    });
    return obj;
  });
}
