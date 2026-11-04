import type { Provider } from './types'

/** A model choice: a provider name, or "<provider>/<model>" for one of the models its server lists. */
export interface ProviderOption { value: string; label: string; disabled?: boolean }

export const shortModel = (m: string) => m.split(/[\\/]/).pop() ?? m

/** The provider a choice belongs to (model ids may contain slashes too). */
export function providerOf(choice: string, providers: Provider[]): Provider | undefined {
  return providers.find((p) => p.name === choice)
    ?? providers.find((p) => choice.startsWith(`${p.name}/`))
}

/**
 * The model menu: one entry per configured provider, except that a server listing several
 * models of its own offers each of them. `listed` holds what each server last reported.
 */
export function providerOptions(providers: Provider[], listed: Record<string, string[]>): ProviderOption[] {
  return providers.flatMap((p): ProviderOption[] => {
    const models = p.lists_models ? listed[p.name] : undefined
    if (models && models.length > 1) {
      return models.map((m) => ({ value: `${p.name}/${m}`, label: `${p.name} · ${shortModel(m)}` }))
    }
    const model = models?.[0] ?? (p.lists_models ? '' : p.model)
    const noKey = p.has_key === false
    return [{ value: p.name, label: `${p.name} · ${model ? shortModel(model) : 'server’s model'}${noKey ? ' (no key)' : ''}`,
              disabled: noKey }]
  })
}

/**
 * Whether the chosen model reads images: as configured for its provider, else as its server
 * said at the last health check (`checked`, by choice). Unknown counts as no.
 */
export function choiceReadsImages(choice: string | null, providers: Provider[], checked: Record<string, boolean>): boolean {
  const provider = choice ? providerOf(choice, providers) : providers.find((p) => p.default)
  if (!provider) return false
  if (provider.supports_images != null) return provider.supports_images
  return checked[choice ?? provider.name] ?? false
}

/**
 * What the menu shows for the current choice. A model chosen while the server offered several
 * stays the choice; while it serves one, that provider entry stands for it (a single-model
 * server answers whatever name is asked for). A model the server no longer offers stays visible
 * as such, so the choice is never silently changed.
 */
export function shownChoice(options: ProviderOption[], choice: string | null, providers: Provider[],
                            listed: Record<string, string[]>): { value: string; missing?: ProviderOption } {
  if (!choice || options.some((o) => o.value === choice)) return { value: choice ?? '' }
  const provider = providerOf(choice, providers)
  const models = provider ? listed[provider.name] ?? [] : []
  if (provider && models.length <= 1) return { value: provider.name }
  if (provider && choice === provider.name) return { value: `${choice}/${models[0]}` }  // what the server runs
  const model = provider ? choice.slice(provider.name.length + 1) : choice
  return { value: choice, missing: { value: choice, label: `${provider?.name ?? choice} · ${shortModel(model)} (not offered)` } }
}

/** A run's model settings in one line: its sampling options, context size and image support. */
export function settingsSummary(settings: Record<string, unknown> | undefined): string {
  if (!settings) return ''
  const options = (settings.request_options ?? {}) as Record<string, unknown>
  const parts = Object.entries(options).filter(([, v]) => v === null || typeof v !== 'object').map(([k, v]) => `${k} ${v}`)
  if (typeof settings.context_tokens === 'number') parts.push(`context ${settings.context_tokens}`)
  if (settings.supports_images === true) parts.push('images')
  return parts.join(' · ')
}
