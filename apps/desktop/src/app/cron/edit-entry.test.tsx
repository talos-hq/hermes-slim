// @vitest-environment jsdom
import { QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import type { CronJob } from '@/hermes'
import { queryClient } from '@/lib/query-client'
import { setCronFocusJobId, setCronJobs } from '@/store/cron'
import { setChangeEventsAvailable } from '@/store/live-sync'

import { CronView } from './index'

const getCronJobs = vi.fn()

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  getCronJobRuns: () => Promise.resolve([]),
  getCronJobs: (profile?: string) => getCronJobs(profile),
  getCronDeliveryTargets: () => Promise.resolve([]),
  getAutomationBlueprints: () => Promise.resolve({ blueprints: [] })
}))

vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))

// A healthy job (no last_error): before the fix its only Edit entry points were the
// last-error banner and the row context menu, so the prompt read as uneditable.
const job: CronJob = {
  deliver: 'local',
  enabled: true,
  id: 'job-kb',
  name: 'Agent knowledge base policy and rollout',
  next_run_at: '2026-11-02T17:00:00Z',
  prompt: 'Define and implement the knowledge base',
  schedule: { expr: '0 9 * * *', kind: 'cron' },
  schedule_display: '0 9 * * *',
  state: 'scheduled'
} as CronJob

beforeAll(() => {
  HTMLElement.prototype.scrollIntoView ??= () => undefined
  vi.stubGlobal('CSS', { escape: (value: string) => value })
})

beforeEach(() => {
  setCronJobs([job])
  setCronFocusJobId(null)
  setChangeEventsAvailable(false)
  getCronJobs.mockResolvedValue([job])
})

afterEach(() => {
  cleanup()
  queryClient.clear()
})

describe('CronView edit entry point', () => {
  it('offers Edit job on a healthy job and opens the editor prefilled with its prompt', async () => {
    await act(async () => {
      render(
        <QueryClientProvider client={queryClient}>
          <CronView onClose={vi.fn()} />
        </QueryClientProvider>
      )
    })

    const edit = await screen.findByRole('button', { name: 'Edit job' })

    await act(async () => {
      edit.click()
    })

    expect(await screen.findByDisplayValue('Define and implement the knowledge base')).toBeTruthy()
    expect(screen.getByDisplayValue('Agent knowledge base policy and rollout')).toBeTruthy()
  })
})
