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

// Legacy allowance for ordinary Next rewrites. File uploads bypass this buffer
// in server.cjs; their effective limit is read by the API at runtime.
const uploadLimitMb = Number(process.env.MAX_UPLOAD_SIZE_MB || '50')
const proxyBodyLimit = (uploadLimitMb + 1) * 1024 * 1024
if (!Number.isSafeInteger(uploadLimitMb) || uploadLimitMb <= 0 ||
    !Number.isSafeInteger(proxyBodyLimit)) {
  throw new Error('MAX_UPLOAD_SIZE_MB must be a positive integer within the safe byte range')
}

const nextConfig = {
  reactStrictMode: true,
  experimental: {
    proxyClientMaxBodySize: proxyBodyLimit,
  },
  allowedDevOrigins,
  // DSH exposes the complete Cangzhi console through a same-origin gateway.
  // Next must know the mount point at build time so client navigation and
  // static asset URLs remain inside that gateway.
  basePath,
  env: {
    NEXT_PUBLIC_CANGZHI_WEB_BASE_PATH: basePath,
    NEXT_PUBLIC_CANGZHI_VERSION: process.env.CANGZHI_VERSION || 'unversioned',
    NEXT_PUBLIC_CANGZHI_REVISION: process.env.CANGZHI_REVISION || 'unknown',
    NEXT_PUBLIC_CANGZHI_BUILD_TIME: process.env.CANGZHI_BUILD_TIME || 'unknown',
    NEXT_PUBLIC_CANGZHI_BUILD_DIRTY: process.env.CANGZHI_BUILD_DIRTY || 'true',
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
