import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { build } from "esbuild";
import { chromium } from "playwright-core";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const sourceFile = path.join(__dirname, "src", "chart.jsx");
const cacheDir = path.join(__dirname, ".cache");
const bundleFile = path.join(cacheDir, "bundle.js");

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const current = argv[i];
    if (!current.startsWith("--")) {
      continue;
    }
    const key = current.slice(2);
    const value = argv[i + 1];
    if (!value || value.startsWith("--")) {
      args[key] = "true";
      continue;
    }
    args[key] = value;
    i += 1;
  }
  return args;
}

function resolveBrowserPath() {
  const candidates = [
    process.env.BIZCHARTS_BROWSER_PATH,
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  ].filter(Boolean);

  const existing = candidates.find((item) => fs.existsSync(item));
  if (!existing) {
    throw new Error(
      "No supported browser found. Set BIZCHARTS_BROWSER_PATH or install Microsoft Edge/Google Chrome.",
    );
  }
  return existing;
}

async function ensureBundle() {
  fs.mkdirSync(cacheDir, { recursive: true });
  const needsRebuild =
    !fs.existsSync(bundleFile) ||
    fs.statSync(bundleFile).mtimeMs < fs.statSync(sourceFile).mtimeMs;

  if (!needsRebuild) {
    return;
  }

  await build({
    entryPoints: [sourceFile],
    bundle: true,
    format: "iife",
    platform: "browser",
    outfile: bundleFile,
    loader: {
      ".js": "jsx",
      ".jsx": "jsx",
    },
    define: {
      "process.env.NODE_ENV": '"production"',
    },
    target: ["chrome109"],
  });
}

function buildHtml(payload) {
  return `<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Server Patrol Chart</title>
    <style>
      html, body {
        margin: 0;
        padding: 0;
        background: #f8fafc;
      }
      #root {
        width: 1280px;
        margin: 0 auto;
      }
    </style>
  </head>
  <body>
    <div id="root"></div>
    <script>
      window.__PATROL_PAYLOAD__ = ${JSON.stringify(payload)};
      window.__PATROL_RENDER_READY__ = false;
    </script>
    <script src="${pathToFileURL(bundleFile).href}"></script>
  </body>
</html>`;
}

async function renderImage(payload, outputPath) {
  const browser = await chromium.launch({
    executablePath: resolveBrowserPath(),
    headless: true,
  });

  const page = await browser.newPage({
    viewport: { width: 1320, height: 760 },
    deviceScaleFactor: 2,
  });
  const diagnostics = [];
  page.on("console", (message) => {
    diagnostics.push(`[console:${message.type()}] ${message.text()}`);
  });
  page.on("pageerror", (error) => {
    diagnostics.push(`[pageerror] ${error.stack || error.message || String(error)}`);
  });

  try {
    const htmlPath = path.join(os.tmpdir(), `server_patrol_${Date.now()}.html`);
    fs.writeFileSync(htmlPath, buildHtml(payload), "utf8");
    await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load" });
    await page.waitForFunction(() => window.__PATROL_RENDER_READY__ === true, null, { timeout: 15000 });
    const capture = await page.locator("#capture");
    await capture.screenshot({
      path: outputPath,
      type: "png",
    });
    fs.unlinkSync(htmlPath);
  } finally {
    if (!fs.existsSync(outputPath) && diagnostics.length > 0) {
      console.error(diagnostics.join("\n"));
    }
    await page.close();
    await browser.close();
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.input || !args.output) {
    throw new Error("Usage: node render.mjs --input <payload.json> --output <chart.png>");
  }

  const payload = JSON.parse(fs.readFileSync(args.input, "utf8"));
  fs.mkdirSync(path.dirname(args.output), { recursive: true });
  await ensureBundle();
  await renderImage(payload, args.output);
  process.stdout.write(args.output);
}

main().catch((error) => {
  console.error(error.stack || String(error));
  process.exit(1);
});
