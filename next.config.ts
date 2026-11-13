import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The tunables file is read at runtime by lib/config; make sure it ships with server bundles.
  outputFileTracingIncludes: { "/**": ["./tunables.toml"] },
};

export default nextConfig;
