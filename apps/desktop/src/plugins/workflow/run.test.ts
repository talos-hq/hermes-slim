import { beforeEach, describe, expect, it, vi } from 'vitest'

const submit = vi.fn<(id: null | string, text: string) => boolean>()
const navigate = vi.fn()

vi.mock('@hermes/plugin-sdk', () => ({ host: { composer: { submit }, navigate } }))

const { runWorkflowInChat, workflowCommand } = await import('./run')

describe('workflow run', () => {
  beforeEach(() => {
    submit.mockReset()
    navigate.mockReset()
  })

  it('builds the slash command the backend parses', () => {
    expect(workflowCommand(' demo ', '')).toBe('/workflow demo')
    expect(workflowCommand('demo', ' the team ')).toBe('/workflow demo the team')
  })

  it('opens a chat and retries until a composer claims the submit', async () => {
    submit.mockReturnValueOnce(false).mockReturnValueOnce(true)
    await expect(runWorkflowInChat('demo', 'x', 5)).resolves.toBe(true)
    expect(navigate).toHaveBeenCalledWith('/')
    expect(submit).toHaveBeenLastCalledWith(null, '/workflow demo x')
  })

  it('reports failure when no composer ever answers', async () => {
    submit.mockReturnValue(false)
    await expect(runWorkflowInChat('demo', '', 2)).resolves.toBe(false)
  })
})
