// 월별 수입횟수 라인차트. recharts(gzip ~154KB)를 이 모듈에만 가두어
// App에서 React.lazy로 지연 로드한다 → 초기 번들에서 recharts가 빠지고,
// 사용자가 상세 뷰의 차트 모달을 열 때 비로소 로드된다.
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, LabelList, ResponsiveContainer,
} from "recharts";

export default function MonthlyLineChart({ data }) {
  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={data} margin={{ top: 28, right: 16, bottom: 4, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey="month" tick={{ fontSize: 10 }} interval="preserveStartEnd" />
        <YAxis tick={{ fontSize: 10 }} allowDecimals={false} width={36} />
        <Tooltip formatter={(v) => [v + "건", "수입횟수"]} contentStyle={{ fontSize: 12, borderRadius: 6 }} />
        <Line
          type="linear" dataKey="count" stroke="#16a34a" strokeWidth={2}
          dot={{ r: 2, fill: "#16a34a" }} activeDot={{ r: 4 }}
        >
          <LabelList dataKey="count" position="top" style={{ fontSize: 10, fill: "#374151", fontWeight: 600 }} formatter={(v) => (v > 0 ? v : "")} />
        </Line>
      </LineChart>
    </ResponsiveContainer>
  );
}
