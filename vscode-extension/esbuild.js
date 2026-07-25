// Two bundles:
//   dist/extension.js   — the extension itself ('vscode' stays external, host-provided)
//   dist/test-bundle.js — the vscode-free modules, so `node --test` can exercise them
const esbuild = require("esbuild");

const watch = process.argv.includes("--watch");

const common = {
  bundle: true,
  format: "cjs",
  platform: "node",
  target: "node20",           // VS Code 1.90+ ships Node 20
  logLevel: "info",
};

const builds = [
  {
    ...common,
    entryPoints: ["src/extension.ts"],
    outfile: "dist/extension.js",
    external: ["vscode"],
    sourcemap: watch ? "inline" : false,
    minify: !watch,
  },
  {
    ...common,
    entryPoints: ["src/testable.ts"],
    outfile: "dist/test-bundle.js",
    sourcemap: "inline",
    minify: false,             // readable stack traces when a test fails
  },
];

(async () => {
  if (watch) {
    for (const options of builds) {
      const ctx = await esbuild.context(options);
      await ctx.watch();
    }
    console.log("watching…");
  } else {
    await Promise.all(builds.map((options) => esbuild.build(options)));
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
