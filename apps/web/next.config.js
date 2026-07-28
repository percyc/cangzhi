/** @type {import('next').NextConfig} */
const allowedDevOrigins = (
  process.env.ALLOWED_DEV_ORIGINS || 'localhost,127.0.0.1,192.168.50.136'
)
  .split(',')
  .map((origin) => origin.trim())
  .filter(Boolean)

const nextConfig = {
  reactStrictMode: true,
  allowedDevOrigins,
  async rewrites() {
    const apiUrl = process.env.API_URL || 'http://localhost:8000'
    return [
      {
        source: '/api/:path*',
        destination: `${apiUrl}/api/:path*`,
      },
    ]
  },
}

module.exports = nextConfig
