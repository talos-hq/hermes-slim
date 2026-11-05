#!/usr/bin/env node
// Build Talos Messaging — the phone PWA — into apps/talos-messaging/dist.
//
// The gateway serves that directory at /talos (hermes_cli/web_server_talos_messaging.py),
// so the build output lives inside the checkout rather than in a swapped
// product directory: an install that later runs `npm run build` in the workspace
// is picked up by the running gateway without a restart.
//
// Icons are a build input, not an artifact of this script: regenerate them with
// `uv run --frozen --group dev python scripts/generate_talos_messaging_icons.py`
// (public/icons/*.png are committed so a plain build never needs a rasterizer).
import { existsSync } from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { repoRoot, workspaceTool } from './frontend-common.mjs'

const workspace = 'apps/talos-messaging'

/** Absolute path to the PWA's workspace inside *source*. */
export function talosMessagingRoot(source = repoRoot) {
  return path.join(source, workspace)
}

/** Build the app; returns the dist directory and its index.html. */
export async function buildTalosMessaging({ source = repoRoot } = {}) {
  const root = talosMessagingRoot(source)
  if (!existsSync(path.join(root, 'package.json'))) {
    throw new Error(`Talos Messaging workspace missing: ${root}`)
  }
  if (!existsSync(path.join(root, 'node_modules'))) {
    throw new Error(
      `Talos Messaging dependencies are not installed: run \`npm install --workspace ${workspace}\``,
    )
  }
  const icons = path.join(root, 'public', 'icons')
  for (const icon of ['apple-touch-icon.png', 'icon-192.png', 'icon-512.png', 'icon-maskable-512.png']) {
    if (!existsSync(path.join(icons, icon))) {
      throw new Error(
        `missing ${icon}: run \`uv run --frozen --group dev python scripts/generate_talos_messaging_icons.py\``,
      )
    }
  }
  const { build } = await import(pathToFileURL(workspaceTool(source, workspace, 'vite')).href)
  const out = path.join(root, 'dist')
  await build({
    root,
    configFile: path.join(root, 'vite.config.ts'),
    configLoader: 'runner',
    build: { outDir: out, emptyOutDir: true },
  })
  const index = path.join(out, 'index.html')
  if (!existsSync(index)) throw new Error('Talos Messaging build did not produce index.html')
  return { out, index }
}

if (path.resolve(process.argv[1] ?? '') === path.resolve(new URL(import.meta.url).pathname)) {
  try {
    const result = await buildTalosMessaging({ source: repoRoot })
    console.log(`built ${result.index}`)
  } catch (error) {
    console.error(error instanceof Error ? error.message : error)
    process.exitCode = 1
  }
}
