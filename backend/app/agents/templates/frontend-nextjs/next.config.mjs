// 정적 내보내기(out/): 서버(Spring·FastAPI)가 화면 파일로 함께 보여 준다. 서버에 붙일 때만 NEXT_PUBLIC_API=server.
// NEXT_BASE_PATH: 하위 주소에서 열 때 (설계 도구의 예시 데이터 미리보기). 개발 중(next dev)에는 /api를 백엔드 8080으로 넘긴다.
const basePath = process.env.NEXT_BASE_PATH || ''
const dev = process.env.NODE_ENV === 'development'

/** @type {import('next').NextConfig} */
export default {
  output: dev ? undefined : 'export',
  basePath,
  assetPrefix: basePath || undefined,
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: false,
  ...(dev ? { rewrites: async () => [{ source: '/api/:path*', destination: 'http://localhost:8080/api/:path*' }] } : {}),
}
