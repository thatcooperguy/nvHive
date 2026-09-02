/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // /query and /council were folded into the chat page in 0.42; keep the old
  // URLs landing for one release. Remove in 0.43.
  async redirects() {
    return [
      { source: '/query', destination: '/', permanent: false },
      { source: '/council', destination: '/?mode=council', permanent: false },
    ];
  },
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: 'http://localhost:8000/:path*',
      },
    ];
  },
};

module.exports = nextConfig;
