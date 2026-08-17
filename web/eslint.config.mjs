import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  globalIgnores([
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Byte-identical ports, following ADR-001's convention for the Python
    // vendored modules: the `diff` against the source repository is what makes
    // the port defensible, and a lint autofix would destroy it. Excluded from
    // the tooling rather than protected by discipline.
    "lib/ndjson.ts",
    "lib/ndjson.test.ts",
  ]),
]);

export default eslintConfig;
