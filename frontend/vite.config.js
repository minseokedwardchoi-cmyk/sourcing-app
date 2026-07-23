import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000'
    }
  },
  build: {
    rollupOptions: {
      output: {
        // 라이브러리를 별도 청크로 분리 → 병렬 다운로드 + 배포 간 캐시 재사용.
        // recharts/react-simple-maps는 상세 뷰에서만 쓰이므로 별도 청크로 두면
        // 지연 로딩(React.lazy) 대상과도 잘 맞는다.
        manualChunks: {
          'react-vendor': ['react', 'react-dom'],
          recharts: ['recharts'],
          maps: ['react-simple-maps', 'd3-geo', 'topojson-client'],
        },
      },
    },
  },
})
