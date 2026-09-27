/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Standalone output: the production image copies only this self-contained
  // server (a pruned node_modules, not the full dev install) rather than the
  // whole project -- see frontend/Dockerfile.
  output: "standalone",
  webpack: (config, { dev }) => {
    // Polling is a dev-only workaround for bind-mounted source on
    // Windows/macOS hosts, where filesystem events do not propagate into the
    // VM reliably. It has no bind mount to compensate for in the production
    // build and would just burn CPU watching files nobody edits.
    if (dev) {
      config.watchOptions = { poll: 1000, aggregateTimeout: 300 };
    }
    return config;
  },
};

module.exports = nextConfig;
