/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // /query and /council were folded into the chat page in 0.42. They were due
  // to go in 0.43; the 0.46 design (O14) re-dated them to retire together with
  // the one-release /wizard redirect that follows the shell merge (0.47 plan,
  // SIMPLIFY_0_47_DESIGN_2026-11.md §3): all three go in 0.48. The /wizard
  // redirect itself is app/wizard/page.tsx (it has to rename ?conversation=
  // to ?c= and keep the other parameters, which a static redirect cannot).
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
        // 127.0.0.1, not localhost: Node resolves localhost to ::1 first on
        // Windows 11 and the API binds IPv4 loopback, so the rewrite would fail.
        destination: 'http://127.0.0.1:8000/:path*',
      },
    ];
  },
};

module.exports = nextConfig;
