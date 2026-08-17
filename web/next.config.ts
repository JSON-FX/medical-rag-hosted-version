import path from "node:path";

import type { NextConfig } from "next";

/**
 * There is deliberately no `NEXT_PUBLIC_API_URL`.
 *
 * In production the frontend and the FastAPI backend deploy as two services in
 * one Vercel project, sharing a domain — so `/api/chat` is same-origin, there
 * is no CORS, and there is no base URL to configure. In development they are
 * two processes on two ports, and this rewrite closes that gap by proxying
 * `/api/*` to the API.
 *
 * The point is that the fetch call is byte-identical in both: an environment
 * variable holding a base URL would add a configuration axis whose only
 * failure mode is silent — pointing a deployed frontend at the wrong backend
 * looks exactly like it working.
 */
const nextConfig: NextConfig = {
  // This app is a subdirectory of a repository whose root is Python, and there
  // is a stray lockfile above the repo on this machine. Pinning the root stops
  // Turbopack inferring a workspace from whichever it finds first.
  turbopack: { root: path.resolve(import.meta.dirname) },

  async rewrites() {
    // Guarded, because in production Vercel's service routing already owns
    // `/api/*`; an unguarded rewrite would send it to a localhost that does
    // not exist there.
    if (process.env.NODE_ENV !== "development") return [];
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8000/api/:path*",
      },
    ];
  },
};

export default nextConfig;
