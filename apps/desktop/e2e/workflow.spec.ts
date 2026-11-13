/**
 * Workflow plugin E2E: the status-bar Workflow button opens the editor, Save
 * writes the YAML through `workflow.save`, Run submits `/workflow <name>` in a
 * chat, and the backend turns it into one turn the (mock) model answers. A
 * typed `/workflow` in the composer takes the same path.
 *
 * Set WORKFLOW_SCREENSHOT to also save the final screenshot at a fixed path.
 * Set HERMES_E2E_CDP=1 where Playwright's `_electron.launch` cannot attach
 * (on a Wayland session main relaunches itself and the target is lost): the same sandbox
 * and mock backend, with the app spawned directly and driven over CDP.
 */

import { type ChildProcess, spawn } from 'node:child_process'
import net from 'node:net'
import path from 'node:path'

import { chromium, type Page } from '@playwright/test'

import { waitForChatReady } from '../../../tests-js/scripts/desktop-chat-smoke'
import { startMockServer } from '../../../tests-js/scripts/mock-server'
import { writeEnvFile, writeMockProviderConfig } from '../../../tests-js/scripts/mock-provider-config'

import {
  buildAppEnv,
  createSandbox,
  findElectron,
  type MockBackendFixture,
  setupMockBackend,
  waitForAppReady
} from './fixtures'
import { expect, test } from './test'

interface Fixture {
  page: Page
  cleanup: () => Promise<void>
}

const freePort = () =>
  new Promise<number>((resolve, reject) => {
    const srv = net.createServer()

    srv.once('error', reject)
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address() as net.AddressInfo

      srv.close(() => resolve(port))
    })
  })

async function setupOverCdp(): Promise<Fixture> {
  const mock = await startMockServer({ replyForPrompt: reply })
  const sandbox = createSandbox('workflow-cdp')

  writeMockProviderConfig(sandbox.hermesHome, mock.url)
  writeEnvFile(sandbox.hermesHome)
  const port = await freePort()

  const child: ChildProcess = spawn(
    findElectron(),
    // An explicit Ozone platform stops main's Wayland self-relaunch, whose
    // parent would keep the debugging port while the window lives in the child.
    [
      '--disable-gpu',
      '--no-sandbox',
      `--ozone-platform=${process.env.WAYLAND_DISPLAY ? 'wayland' : 'x11'}`,
      `--remote-debugging-port=${port}`,
      '.'
    ],
    {
      cwd: path.resolve(import.meta.dirname, '..'),
      env: { ...buildAppEnv(sandbox), TEST_WORKER_INDEX: '0' },
      stdio: 'ignore'
    }
  )

  let browser: Awaited<ReturnType<typeof chromium.connectOverCDP>> | null = null

  for (let i = 0; i < 120 && !browser; i += 1) {
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`).catch(() => null)

    if (!browser) {
      await new Promise(r => setTimeout(r, 500))
    }
  }

  if (!browser) {
    throw new Error('Electron never exposed CDP')
  }

  let page: Page | undefined

  for (let i = 0; i < 120 && !page; i += 1) {
    page = browser
      .contexts()
      .flatMap(c => c.pages())
      .find(p => /^(file|app|hermes|http):/.test(p.url()) && !p.url().includes('preview'))

    if (!page) {
      await new Promise(r => setTimeout(r, 500))
    }
  }

  if (!page) {
    throw new Error('no app window over CDP')
  }

  await waitForChatReady(page, 120_000)

  return {
    page,
    cleanup: async () => {
      await browser?.close().catch(() => undefined)
      child.kill('SIGTERM')
      await mock.close()
      sandbox.cleanup()
    }
  }
}

async function setupViaPlaywright(): Promise<Fixture> {
  const f: MockBackendFixture = await setupMockBackend({ mockServer: { replyForPrompt: reply } })

  await waitForAppReady(f, 120_000)

  return f
}

const DONE = 'Workflow complete: greeted the team and signed off.'

const reply = (prompt: string) =>
  prompt.includes('[/workflow hello-workflow]') && prompt.includes('Step 2: Sign off')
    ? `## Step 1: Greet\nHello, team!\n\n## Step 2: Sign off\nSee you soon.\n\n## Workflow complete\n${DONE}`
    : 'not a workflow turn'

let fixture: Fixture | null = null

test.beforeAll(async () => {
  test.setTimeout(240_000)
  fixture = process.env.HERMES_E2E_CDP === '1' ? await setupOverCdp() : await setupViaPlaywright()
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('Workflow button opens the editor and Run executes the workflow in chat', async () => {
  const page = fixture!.page

  await page.getByTestId('workflow-button').click()
  await expect(page.getByTestId('workflow-editor')).toBeVisible()
  await page.getByLabel('Workflow input').fill('the team')
  await page.getByTestId('workflow-run').click()

  await expect(page.getByText(DONE).first()).toBeVisible({ timeout: 60_000 })
  await expect(page.getByTestId('workflow-button')).toBeVisible()

  const path = process.env.WORKFLOW_SCREENSHOT || test.info().outputPath('workflow-run.png')

  await page.screenshot({ path })
})

test('typing /workflow in the composer runs the saved workflow', async () => {
  const page = fixture!.page
  const composer = page.locator('[contenteditable="true"]').first()

  await page.getByTestId('workflow-button').click()
  await page.getByTestId('workflow-run').waitFor()
  await page.evaluate(() => (window.location.hash = '#/'))
  await composer.click()
  await composer.type('/workflow hello-workflow the team')
  await page.keyboard.press('Enter')
  await expect(page.getByText(DONE).first()).toBeVisible({ timeout: 60_000 })
})
