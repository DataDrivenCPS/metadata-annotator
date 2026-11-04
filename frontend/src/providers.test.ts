import { describe, expect, it } from 'vitest'
import { providerOf, providerOptions, shownChoice } from './providers'
import type { Provider } from './types'

const provider = (name: string, model: string, extra: Partial<Provider> = {}): Provider => ({
  name, kind: 'openai', model, base_url: null, supports_images: true, default: false, has_key: null, ...extra })
const providers = [provider('local', '', { lists_models: true }), provider('openrouter', 'google/gemma-4-26b', { has_key: false })]

describe('model menu', () => {
  it('offers each model a server lists, and configured providers as they are', () => {
    expect(providerOptions(providers, { local: ['gemma-3-4b', 'unsloth/qwen3-vl-8b:Q4_K_M'] })).toEqual([
      { value: 'local/gemma-3-4b', label: 'local · gemma-3-4b' },
      { value: 'local/unsloth/qwen3-vl-8b:Q4_K_M', label: 'local · qwen3-vl-8b:Q4_K_M' },
      { value: 'openrouter', label: 'openrouter · gemma-4-26b (no key)', disabled: true },
    ])
  })

  it('names a single-model server by what it serves, or says it will ask the server', () => {
    expect(providerOptions(providers, { local: ['gemma-3-4b'] })[0]).toEqual({ value: 'local', label: 'local · gemma-3-4b', disabled: false })
    expect(providerOptions(providers, {})[0].label).toBe('local · server’s model')
  })

  it('finds the provider of a model choice even when the model id has slashes', () => {
    expect(providerOf('local/unsloth/qwen3-vl-8b', providers)?.name).toBe('local')
    expect(providerOf('elsewhere/x', providers)).toBeUndefined()
  })

  it('keeps a remembered model choice visible instead of silently switching it', () => {
    const several = { local: ['gemma-3-4b', 'qwen3-vl-8b'] }
    const options = providerOptions(providers, several)
    expect(shownChoice(options, 'local/qwen3-vl-8b', providers, several)).toEqual({ value: 'local/qwen3-vl-8b' })
    expect(shownChoice(options, 'local', providers, several)).toEqual({ value: 'local/gemma-3-4b' })  // what the server runs
    expect(shownChoice(options, 'local/gone', providers, several).missing?.label).toBe('local · gone (not offered)')
    const one = { local: ['gemma-3-4b'] }
    expect(shownChoice(providerOptions(providers, one), 'local/qwen3-vl-8b', providers, one)).toEqual({ value: 'local' })
  })
})

describe('reading images', () => {
  it('follows the chosen model as its server reported it, unless the provider configures it', async () => {
    const { choiceReadsImages } = await import('./providers')
    const list = [provider('local', '', { lists_models: true, supports_images: null }),
                  provider('claude', 'claude-opus-5-5', { kind: 'anthropic', supports_images: true, default: true }),
                  provider('pinned', '', { lists_models: true, supports_images: false })]
    const checked = { 'local/gemma-3-4b': true, 'local/text-only': false, pinned: true }
    expect(choiceReadsImages('local/gemma-3-4b', list, checked)).toBe(true)
    expect(choiceReadsImages('local/text-only', list, checked)).toBe(false)
    expect(choiceReadsImages('local/not-checked-yet', list, checked)).toBe(false)
    expect(choiceReadsImages('pinned', list, checked)).toBe(false)  // configured: the server is not asked
    expect(choiceReadsImages(null, list, checked)).toBe(true)  // nothing chosen: the default provider
  })
})
