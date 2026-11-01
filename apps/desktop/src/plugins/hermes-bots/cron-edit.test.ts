import { describe, expect, it } from 'vitest'

import { routinePrompt } from './cron'
import { routineFormFromJob, routineInstruction, routineRepeatTimes, routineUpdateParams } from './cron-edit'
import type { RoutineJob } from './types'

describe('routineInstruction', () => {
  it("unwraps a delegated routine's instruction, quotes included", () => {
    const instruction = `say "it's done" & exit`

    expect(routineInstruction(routinePrompt('botb', 'T', instruction, 'default'))).toBe(instruction)
  })

  it('returns a plain prompt unchanged', () => {
    expect(routineInstruction('just do it')).toBe('just do it')
  })
})

describe('routineRepeatTimes', () => {
  it.each([
    ['forever', ''],
    ['once', '1'],
    ['5 times', '5'],
    ['2/5', '5'],
    [3, '3']
  ] as const)('%s → %s', (repeat, times) => {
    expect(routineRepeatTimes(repeat)).toBe(times)
  })
})

const job: RoutineJob = {
  job_id: 'j1',
  name: '[bot:botb] KB policy',
  prompt: 'Define the knowledge base',
  repeat: 'forever',
  schedule: 'every 120m'
}

describe('routineUpdateParams', () => {
  it('sends nothing for an untouched form', () => {
    const form = routineFormFromJob(job)

    expect(routineUpdateParams(job, form, { ...form }, 'botb', 'botb')).toEqual({})
  })

  it('sends only the changed fields and keeps the bot tag on a rename', () => {
    const form = routineFormFromJob(job)

    expect(
      routineUpdateParams(
        job,
        form,
        { ...form, instruction: 'New body', schedule: '0 9 * * 1-5', title: 'KB v2', workdir: '/srv' },
        'botb',
        'botb'
      )
    ).toEqual({ name: '[bot:botb] KB v2', prompt: 'New body', schedule: '0 9 * * 1-5', workdir: '/srv' })
  })

  it('rewraps a delegated prompt when only the title changes', () => {
    const wrapped = { ...job, prompt: routinePrompt('botb', 'KB policy', 'Define', 'default') }
    const form = routineFormFromJob(wrapped)
    const params = routineUpdateParams(wrapped, form, { ...form, title: 'KB v2' }, 'botb', 'default')

    expect(params.prompt).toBe(routinePrompt('botb', 'KB v2', 'Define', 'default'))
  })
})
