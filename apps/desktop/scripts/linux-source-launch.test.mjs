import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'
import vm from 'node:vm'

test('a launcher click moves the existing chat here before activation', () => {
  const currentDesktop = { id: 'clicked-desktop' }
  const chat = { normalWindow: true, resourceClass: 'hermes', desktops: [{ id: 'old' }], minimized: true }
  const other = { normalWindow: true, resourceClass: 'editor', desktops: [{ id: 'old' }] }
  const helper = { normalWindow: false, resourceClass: 'hermes', desktops: [{ id: 'old' }] }
  const workspace = {
    currentDesktop,
    windowList: () => [other, helper, chat],
    set activeWindow(window) {
      assert.equal(window, chat)
      assert.equal(window.desktops.length, 1)
      assert.equal(window.desktops[0], currentDesktop)
      assert.equal(window.minimized, false)
      this.activated = window
    },
  }
  vm.runInNewContext(readFileSync(new URL('./linux-summon.js', import.meta.url), 'utf8'), { workspace })
  assert.equal(workspace.activated, chat)
  assert.equal(workspace.currentDesktop, currentDesktop)
  assert.equal(other.desktops[0].id, 'old')
  assert.equal(helper.desktops[0].id, 'old')
})

test('every chat window moves here so focusing the main window cannot switch desktops', () => {
  const currentDesktop = { id: 'clicked-desktop' }
  const old = { id: 'old' }
  const popout = { normalWindow: true, resourceClass: 'hermes', desktops: [old] }
  const main = { normalWindow: true, resourceClass: 'hermes', desktops: [old] }
  const pinned = { normalWindow: true, resourceClass: 'hermes', onAllDesktops: true, desktops: [old] }
  const quickEntry = { normalWindow: true, resourceClass: 'hermes', skipTaskbar: true, desktops: [old] }
  const workspace = { currentDesktop, windowList: () => [popout, main, pinned, quickEntry], activeWindow: null }
  vm.runInNewContext(readFileSync(new URL('./linux-summon.js', import.meta.url), 'utf8'), { workspace })
  assert.equal(popout.desktops[0], currentDesktop)
  assert.equal(main.desktops[0], currentDesktop)
  assert.equal(pinned.desktops[0], old)
  assert.equal(quickEntry.desktops[0], old)
})

test('a cold launch leaves other applications alone', () => {
  const workspace = { currentDesktop: { id: 'here' }, windowList: () => [], activeWindow: null }
  vm.runInNewContext(readFileSync(new URL('./linux-summon.js', import.meta.url), 'utf8'), { workspace })
  assert.equal(workspace.activeWindow, null)
})

test.skipIf(process.platform !== 'linux')('the desktop process receives this checkout and its backend override', () => {
  const scratch = mkdtempSync(path.join(os.tmpdir(), 'hermes-launch-test-'))
  const root = path.join(scratch, 'checkout')
  const script = path.join(root, 'apps/desktop/scripts/linux-source-launch.mjs')
  const fake = path.join(scratch, 'electron')
  const output = path.join(scratch, 'launch.json')
  try {
    mkdirSync(path.dirname(script), { recursive: true })
    mkdirSync(path.join(root, 'apps/desktop/dist'), { recursive: true })
    mkdirSync(path.join(root, '.venv/bin'), { recursive: true })
    writeFileSync(path.join(root, 'apps/desktop/dist/electron-main.mjs'), '')
    writeFileSync(path.join(root, '.venv/bin/hermes'), '')
    writeFileSync(script, readFileSync(new URL('./linux-source-launch.mjs', import.meta.url)))
    writeFileSync(fake, `#!${process.execPath}\nrequire('fs').writeFileSync(process.env.LAUNCH_RESULT, JSON.stringify({args:process.argv.slice(2),root:process.env.HERMES_DESKTOP_HERMES_ROOT,cli:process.env.HERMES_DESKTOP_HERMES}))\n`)
    chmodSync(fake, 0o700)
    execFileSync(process.execPath, [script, fake, '--example'], {
      env: { ...process.env, HERMES_DESKTOP_PYTHON: '', XDG_CURRENT_DESKTOP: '', LAUNCH_RESULT: output },
    })
    assert.deepEqual(JSON.parse(readFileSync(output, 'utf8')), {
      args: [path.join(root, 'apps/desktop'), '--example'], root, cli: path.join(root, '.venv/bin/hermes'),
    })
  } finally {
    rmSync(scratch, { recursive: true, force: true })
  }
})
