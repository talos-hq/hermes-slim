import { describe, expect, it } from 'vitest'

import type { CronJob } from '@/hermes'

import { moreFieldsFromJob, moreFieldsUpdates } from './more-fields'

const job = {
  context_from: ['self', 'other-job'],
  enabled: true,
  enabled_toolsets: ['web', 'terminal'],
  id: 'job-1',
  repeat: { completed: 1, times: 5 },
  skills: ['alpha'],
  workdir: '/srv/app'
} as CronJob

describe('moreFieldsFromJob', () => {
  it('prefills every field from the stored job', () => {
    const state = moreFieldsFromJob(job)

    expect(state.skills).toBe('alpha')
    expect(state.repeat).toBe('5')
    expect(state.toolsets).toBe('web, terminal')
    expect(state.workdir).toBe('/srv/app')
    // continuity's own ref is not shown as an external context source
    expect(state.contextFrom).toBe('other-job')
  })

  it('shows forever as a blank repeat', () => {
    expect(moreFieldsFromJob({ ...job, repeat: { times: null } }).repeat).toBe('')
  })
})

describe('moreFieldsUpdates', () => {
  it('sends nothing when nothing changed', () => {
    const state = moreFieldsFromJob(job)

    expect(moreFieldsUpdates(state, { ...state }, job)).toEqual({})
  })

  it('sends only changed fields, coerced to the API shape', () => {
    const initial = moreFieldsFromJob(job)

    const updates = moreFieldsUpdates(
      initial,
      { ...initial, repeat: '', skills: 'alpha, beta ,', toolsets: '', workdir: '', contextFrom: 'x' },
      job
    )

    expect(updates).toEqual({
      context_from: ['self', 'x'],
      enabled_toolsets: null,
      repeat: 0,
      skills: ['alpha', 'beta'],
      workdir: ''
    })
  })
})
