/**
 * countryGeo.js — 한글 국가명 ↔ 영문(지도 데이터 표준) 국가명 매핑
 * world-atlas(countries-110m) topojson의 properties.name 값과 매칭하기 위한 테이블.
 * DB의 country 값은 한글 국가명(예: "미국")이므로, 지도 위 영문 국가명을 거쳐
 * 역매핑하여 DB 보유 여부를 판단하고 상세 페이지로 이동한다.
 */
export const KOREAN_TO_ENGLISH = {
  "대한민국": "South Korea", "한국": "South Korea", "북한": "North Korea", "조선": "North Korea",
  "미국": "United States of America", "캐나다": "Canada", "멕시코": "Mexico",
  "중국": "China", "일본": "Japan", "대만": "Taiwan", "홍콩": "Hong Kong S.A.R.", "마카오": "Macau",
  "몽골": "Mongolia", "베트남": "Vietnam", "태국": "Thailand", "인도네시아": "Indonesia",
  "말레이지아": "Malaysia", "말레이시아": "Malaysia", "필리핀": "Philippines", "싱가포르": "Singapore",
  "미얀마": "Myanmar", "캄보디아": "Cambodia", "라오스": "Laos", "브루나이": "Brunei",
  "인도": "India", "파키스탄": "Pakistan", "방글라데시": "Bangladesh", "스리랑카": "Sri Lanka",
  "네팔": "Nepal", "부탄": "Bhutan", "아프가니스탄": "Afghanistan",
  "카자흐스탄": "Kazakhstan", "우즈베키스탄": "Uzbekistan", "투르크메니스탄": "Turkmenistan",
  "키르기스스탄": "Kyrgyzstan", "타지키스탄": "Tajikistan",
  "러시아": "Russia", "러시아연방": "Russia",
  "영국": "United Kingdom", "프랑스": "France", "독일": "Germany", "이탈리아": "Italy",
  "스페인": "Spain", "포르투갈": "Portugal", "네덜란드": "Netherlands", "벨기에": "Belgium",
  "스위스": "Switzerland", "오스트리아": "Austria", "스웨덴": "Sweden", "노르웨이": "Norway",
  "덴마크": "Denmark", "핀란드": "Finland", "아이슬란드": "Iceland", "아일랜드": "Ireland",
  "폴란드": "Poland", "체코": "Czechia", "슬로바키아": "Slovakia", "헝가리": "Hungary",
  "루마니아": "Romania", "불가리아": "Bulgaria", "그리스": "Greece", "터키": "Turkey", "튀르키예": "Turkey",
  "우크라이나": "Ukraine", "벨라루스": "Belarus", "리투아니아": "Lithuania", "라트비아": "Latvia",
  "에스토니아": "Estonia", "크로아티아": "Croatia", "세르비아": "Serbia", "슬로베니아": "Slovenia",
  "보스니아헤르체고비나": "Bosnia and Herzegovina", "알바니아": "Albania",
  "북마케도니아": "North Macedonia", "몰도바": "Moldova", "몰타": "Malta", "룩셈부르크": "Luxembourg",
  "키프로스": "Cyprus", "조지아": "Georgia", "아르메니아": "Armenia", "아제르바이잔": "Azerbaijan",
  "호주": "Australia", "뉴질랜드": "New Zealand", "피지": "Fiji", "파푸아뉴기니": "Papua New Guinea",
  "브라질": "Brazil", "아르헨티나": "Argentina", "칠레": "Chile", "페루": "Peru",
  "콜롬비아": "Colombia", "베네수엘라": "Venezuela", "에콰도르": "Ecuador", "볼리비아": "Bolivia",
  "파라과이": "Paraguay", "우루과이": "Uruguay", "가이아나": "Guyana", "수리남": "Suriname",
  "남아프리카": "South Africa", "남아프리카공화국": "South Africa", "이집트": "Egypt",
  "나이지리아": "Nigeria", "케냐": "Kenya", "에티오피아": "Ethiopia", "탄자니아": "United Republic of Tanzania",
  "모로코": "Morocco", "알제리": "Algeria", "튀니지": "Tunisia", "리비아": "Libya",
  "가나": "Ghana", "코트디부아르": "Ivory Coast", "세네갈": "Senegal", "카메룬": "Cameroon",
  "우간다": "Uganda", "짐바브웨": "Zimbabwe", "잠비아": "Zambia", "모잠비크": "Mozambique",
  "나미비아": "Namibia", "보츠와나": "Botswana", "마다가스카르": "Madagascar",
  "수단": "Sudan", "남수단": "South Sudan", "콩고민주공화국": "Democratic Republic of the Congo",
  "콩고": "Republic of Congo", "앙골라": "Angola", "말리": "Mali", "니제르": "Niger", "차드": "Chad",
  "사우디아라비아": "Saudi Arabia", "아랍에미리트": "United Arab Emirates", "카타르": "Qatar",
  "쿠웨이트": "Kuwait", "오만": "Oman", "바레인": "Bahrain", "예멘": "Yemen",
  "이스라엘": "Israel", "팔레스타인": "Palestine", "요르단": "Jordan", "레바논": "Lebanon",
  "시리아": "Syria", "이라크": "Iraq", "이란": "Iran",
  "쿠바": "Cuba", "자메이카": "Jamaica", "도미니카공화국": "Dominican Republic", "아이티": "Haiti",
  "파나마": "Panama", "코스타리카": "Costa Rica", "과테말라": "Guatemala", "온두라스": "Honduras",
  "엘살바도르": "El Salvador", "니카라과": "Nicaragua", "벨리즈": "Belize",
};

// 여러 한글 별칭이 같은 영문명으로 매핑되는 경우(예: "러시아"/"러시아연방",
// "남아프리카"/"남아프리카공화국")를 대비해 영문명 → 한글 별칭 "목록"을 만든다.
// 단순 역매핑(영문명 → 마지막 별칭 하나)만 쓰면, DB에 실제 저장된 별칭과
// 지도가 확인하는 별칭이 달라서 데이터가 있어도 매칭에 실패할 수 있다.
const ENGLISH_TO_KOREAN_ALIASES = {};
for (const [ko, en] of Object.entries(KOREAN_TO_ENGLISH)) {
  (ENGLISH_TO_KOREAN_ALIASES[en] ??= []).push(ko);
}

export const ENGLISH_TO_KOREAN = Object.fromEntries(
  Object.entries(ENGLISH_TO_KOREAN_ALIASES).map(([en, aliases]) => [en, aliases[0]])
);

/** 지도 라벨 등 대표 이름 하나만 필요할 때 (DB 보유 여부와 무관). */
export function getKoreanName(englishGeoName) {
  return ENGLISH_TO_KOREAN[englishGeoName] || null;
}

/**
 * DB(dbCountries)에 실제로 존재하는 별칭을 우선 반환한다.
 * 일치하는 별칭이 없으면 대표 이름(getKoreanName과 동일)을 반환한다.
 */
export function resolveKoreanName(englishGeoName, dbCountries) {
  const aliases = ENGLISH_TO_KOREAN_ALIASES[englishGeoName];
  if (!aliases) return null;
  if (dbCountries) {
    const match = aliases.find(a => dbCountries.has(a));
    if (match) return match;
  }
  return aliases[0];
}
