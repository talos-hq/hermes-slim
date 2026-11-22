import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import { listOAuthProviders, setupCludProvider } from '@/hermes'
import { setMainModelAssignment } from '@/store/model-assignment'

import {
  $desktopOnboarding,
  closeManualOnboarding,
  confirmOnboardingModel,
  refreshOnboardingProviders,
  startCludOnboarding
} from './onboarding'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  listOAuthProviders: vi.fn(),
  setupCludProvider: vi.fn()
}))
vi.mock('@/store/model-assignment', () => ({ setMainModelAssignment: vi.fn() }))

const initial = $desktopOnboarding.get()
const scope = { connectionId: 'fixture-connection', profile: 'fixture-profile' }
const ctx = { scope, requestGateway: vi.fn() }

beforeEach(() => {
  $desktopOnboarding.set({ ...initial, configured: false, manual: false, flow: { status: 'idle' } })
  vi.mocked(setupCludProvider).mockResolvedValue({ provider: 'clud-codex', models: ['fixture-a', 'fixture-b'] })
  vi.mocked(setMainModelAssignment).mockResolvedValue({ ok: true } as never)
})
afterEach(() => {
  closeManualOnboarding()
  $desktopOnboarding.set(initial)
  vi.clearAllMocks()
})

describe('Codex via clud onboarding', () => {
  it('keeps the clud choice available when the backend has no OAuth providers', async () => {
    vi.mocked(listOAuthProviders).mockResolvedValue({ providers: [], clud_available: true })
    await refreshOnboardingProviders()
    expect($desktopOnboarding.get()).toMatchObject({ mode: 'oauth', providers: [], cludAvailable: true })
  })

  it('uses the scoped setup route and confirms a discovered model', async () => {
    await startCludOnboarding(ctx)
    expect(setupCludProvider).toHaveBeenCalledWith(scope)
    expect(setMainModelAssignment).toHaveBeenCalledWith({ provider: 'clud-codex', model: 'fixture-a' }, scope, {
      skipConfirmPrompt: true
    })
    expect($desktopOnboarding.get().flow).toMatchObject({
      status: 'confirming_model',
      providerSlug: 'clud-codex',
      currentModel: 'fixture-a'
    })
  })

  it('keeps the existing default during manual setup', async () => {
    $desktopOnboarding.set({ ...$desktopOnboarding.get(), manual: true })
    await startCludOnboarding(ctx)
    expect(setMainModelAssignment).not.toHaveBeenCalled()
    expect($desktopOnboarding.get().flow.status).toBe('confirming_model')
    await confirmOnboardingModel(ctx)
    expect(setMainModelAssignment).toHaveBeenCalledWith({ provider: 'clud-codex', model: 'fixture-a' }, scope, {
      skipConfirmPrompt: true
    })
  })

  it('shows the server command when discovery fails', async () => {
    vi.mocked(setupCludProvider).mockRejectedValue(new Error('Run clud serve --codex, then retry setup.'))
    await startCludOnboarding(ctx)
    expect($desktopOnboarding.get().flow).toMatchObject({
      status: 'error',
      message: expect.stringContaining('clud serve --codex')
    })
    expect(setMainModelAssignment).not.toHaveBeenCalled()
  })

  it('discards setup completed after the picker closes', async () => {
    let resolve!: (value: { provider: string; models: string[] }) => void
    vi.mocked(setupCludProvider).mockReturnValue(
      new Promise(done => {
        resolve = done
      })
    )
    const pending = startCludOnboarding(ctx)
    closeManualOnboarding()
    resolve({ provider: 'clud-codex', models: ['fixture-a'] })
    await pending
    expect(setMainModelAssignment).not.toHaveBeenCalled()
    expect($desktopOnboarding.get().flow.status).toBe('idle')
  })
})
