/** @type {import('next').NextConfig} */
const allowedDevOrigins = (
  process.env.ALLOWED_DEV_ORIGINS || 'localhost,127.0.0.1,192.168.50.136'
)
  .split(',')
  .map((origin) => origin.trim())
  .filter(Boolean)

const configuredBasePath = (process.env.CANGZHI_WEB_BASE_PATH || '').trim()
const basePath = configuredBasePath && configuredBasePath !== '/'
  ? `/${configuredBasePath.replace(/^\/+|\/+$/g, '')}`
  : ''

const nextConfig = {
  reactStrictMode: true,
  allowedDevOrigins,
  // DSH exposes the complete Cangzhi console through a same-origin gateway.
  // Next must know the mount point at build time so client navigation and
  // static asset URLs remain inside that gateway.
  basePath,
  env: {
    NEXT_PUBLIC_CANGZHI_WEB_BASE_PATH: basePath,
  },
  async redirects() {
    if (!basePath) return []
    return [
      {
        source: '/',
        destination: `${basePath}/documents`,
        permanent: false,
        basePath: false,
      },
    ]
  },
  async rewrites() {
    const apiUrl = process.env.API_URL || 'http://localhost:8000'
    const routes = [
      {
        source: '/api/:path*',
        destination: `${apiUrl}/api/:path*`,
      },
    ]
    if (basePath) {
      routes.push({
        source: '/api/:path*',
        destination: `${apiUrl}/api/:path*`,
        basePath: false,
      })
    }
    return routes
  },
}

module.exports = nextConfig
