#!/usr/bin/env node
// Launch this checkout's Desktop and backend, including from Plasma's panel.
import { execFileSync, spawn, spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(here, '../../..')
const electron = process.argv[2] || 'electron'
const python = process.env.HERMES_DESKTOP_PYTHON || path.join(root, '.venv/bin/python')
const hermes = path.join(path.dirname(python), 'hermes')

if (!existsSync(path.join(root, 'apps/desktop/dist/electron-main.mjs'))) {
  throw new Error('Build this checkout first: npm --prefix apps/desktop run build')
}
if (!existsSync(hermes)) {
  throw new Error('Prepare this checkout’s Python environment before launching Desktop')
}

if (process.env.XDG_CURRENT_DESKTOP?.split(':').includes('KDE')) {
  const name = `hermes-summon-${process.pid}`
  // Plasma 6 distributions often install only the qdbus6 name.
  const qdbus = ['qdbus', 'qdbus6'].find(tool => spawnSync(tool, ['--version'], { stdio: 'ignore' }).error === undefined)
  const call = (...args) => execFileSync(qdbus, ['org.kde.KWin', ...args], {
    encoding: 'utf8', timeout: 5000,
  }).trim()
  // The summon only moves windows; failing it must never stop Hermes from opening.
  try {
    if (!qdbus) throw new Error('neither qdbus nor qdbus6 is installed')
    try {
      const id = call('/Scripting', 'org.kde.kwin.Scripting.loadScript', path.join(here, 'linux-summon.js'), name)
      if (!/^\d+$/.test(id)) throw new Error(`KWin refused the summon script: ${id}`)
      call(`/Scripting/Script${id}`, 'org.kde.kwin.Script.run')
    } finally {
      call('/Scripting', 'org.kde.kwin.Scripting.unloadScript', name)
    }
  } catch (error) {
    console.error(`[hermes] could not summon windows to this desktop: ${error.message}`)
  }
}

const child = spawn(electron, [path.join(root, 'apps/desktop'), ...process.argv.slice(3)], {
  stdio: 'inherit',
  env: {
    ...process.env,
    HERMES_DESKTOP_HERMES_ROOT: root,
    HERMES_DESKTOP_HERMES: hermes,
    HERMES_DESKTOP_PYTHON: python,
  },
})
child.on('error', error => { console.error(error.message); process.exitCode = 1 })
child.on('exit', code => { process.exitCode = code ?? 1 })
