import { FlatCompat } from "@eslint/eslintrc";

// `eslint-config-next` still ships as a legacy (`.eslintrc`-shaped) config.
// `FlatCompat` is the documented bridge Next.js itself recommends for using
// it under ESLint 9's flat config -- there is no flat-native build of
// `eslint-config-next` yet, and hand-porting its rule set would drift from
// whatever Next actually ships on the next upgrade.
const compat = new FlatCompat({ baseDirectory: import.meta.dirname });

const config = [
  {
    // next-env.d.ts is generated and rewritten by `next build`/`next dev` on
    // every run; its triple-slash reference is Next's own required syntax,
    // not something this project's code style controls.
    ignores: [".next/**", "node_modules/**", "next-env.d.ts"],
  },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
];

export default config;
