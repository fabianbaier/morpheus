#!/usr/bin/env node
// Generate the deployable EvenHub manifest (app.build.json) from the app.json
// template, injecting the real bridge host into the `network` permission
// whitelist. On the glasses the mini app runs on the phone and must reach the
// laptop bridge over Tailscale, so the whitelist has to name that host — the
// checked-in app.json only lists localhost for local browser dev.
//
//   MORPHEUS_G2_PUBLIC_URL=https://your-mac.your-tailnet.ts.net \
//     node scripts/build-manifest.mjs
//
// Writes app.build.json next to app.json; `evenhub pack app.build.json dist`
// consumes it.
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const templatePath = join(root, "app.json");
const outPath = join(root, "app.build.json");

// Default matches the Makefile's G2_PUBLIC_URL so `make g2-app-pack` is
// consistent; override per-invocation with MORPHEUS_G2_PUBLIC_URL.
const publicUrl = (
  process.env.MORPHEUS_G2_PUBLIC_URL ||
  "https://fabians-macbook-pro.tail3387a8.ts.net"
).replace(/\/+$/, "");

const manifest = JSON.parse(readFileSync(templatePath, "utf8"));
const devHosts = ["http://127.0.0.1:3456", "http://localhost:3456"];
const whitelist = [...new Set([publicUrl, ...devHosts])];

const network = (manifest.permissions || []).find((p) => p?.name === "network");
if (!network) {
  console.error("app.json has no `network` permission to populate");
  process.exit(1);
}
network.whitelist = whitelist;

writeFileSync(outPath, `${JSON.stringify(manifest, null, 2)}\n`);
console.log(`Wrote ${outPath}`);
console.log(`  bridge whitelist: ${whitelist.join(", ")}`);
