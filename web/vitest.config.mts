import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    // jsdom rather than node, unlike the source repository this ports from:
    // two of the acceptance criteria are about what a component renders, and
    // AnswerText's citation mapping is the single assumption the whole
    // citation feature rests on. It gets a real test.
    environment: "jsdom",
    include: ["lib/**/*.test.ts", "components/**/*.test.tsx"],
    globals: true,
  },
  resolve: {
    alias: { "@": new URL(".", import.meta.url).pathname },
  },
});
