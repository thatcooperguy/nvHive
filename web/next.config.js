/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // /query and /council were folded into the chat page in 0.42. They were due
  // to go in 0.43; the 0.46 design (O14) re-dated them to retire together with
  // the one-release /wizard redirect that follows the shell merge (0.47 plan,
  // SIMPLIFY_0_47_DESIGN_2026-11.md §3): all three go in 0.48.
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
