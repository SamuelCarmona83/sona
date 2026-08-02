import type {
  CatalogAlbum,
  CatalogArtist,
  CatalogSummary,
  CatalogTrack,
  DedupePreview,
  DiskUsage,
} from '../types'

export interface CatalogPayload {
  generated_at?: number
  summary?: CatalogSummary
  tracks?: CatalogTrack[]
  artists?: CatalogArtist[]
  albums?: CatalogAlbum[]
}

const CACHE_DIRS = ['.cache', 'spotify_cache'] as const

let stickyCacheDir: string | null = null

export function getCacheDir(): string | null {
  return stickyCacheDir
}

export async function loadCacheJson(filename: string): Promise<unknown | null> {
  const dirs = stickyCacheDir
    ? [stickyCacheDir, ...CACHE_DIRS.filter((d) => d !== stickyCacheDir)]
    : [...CACHE_DIRS]

  for (const dir of dirs) {
    try {
      const res = await fetch(`/${dir}/${filename}`)
      if (res.ok) {
        stickyCacheDir = dir
        return await res.json()
      }
    } catch {
      /* try next */
    }
  }
  return null
}

export async function loadDiskUsage(): Promise<DiskUsage> {
  try {
    const res = await fetch('/api/disk-usage')
    if (res.ok) return (await res.json()) as DiskUsage
  } catch {
    /* static fallback */
  }
  return { total_bytes: 0, files: {}, tracks_on_disk: 0 }
}

export async function loadDedupePreview(): Promise<DedupePreview | null> {
  try {
    const res = await fetch('/api/library/dedupe-preview')
    if (res.ok) return (await res.json()) as DedupePreview
  } catch {
    /* API unavailable */
  }
  return null
}

export async function deleteLibraryTrack(trackId: string): Promise<{
  ok: boolean
  status: number
  data: Record<string, unknown>
}> {
  const res = await fetch('/api/library/track/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ track_id: trackId }),
  })
  const data = (await res.json().catch(() => ({}))) as Record<string, unknown>
  return { ok: res.ok, status: res.status, data }
}

export async function runDedupe(): Promise<Record<string, unknown>> {
  const res = await fetch('/api/library/dedupe', { method: 'POST' })
  const data = (await res.json()) as Record<string, unknown>
  if (!res.ok) throw new Error(String(data.error || 'error al limpiar'))
  return data
}

export async function enrichPreview(): Promise<{
  suggest_enrich?: number
  [key: string]: unknown
}> {
  return fetch('/api/library/enrich-preview').then((r) => r.json())
}

export async function runEnrich(): Promise<Record<string, unknown>> {
  const res = await fetch('/api/library/enrich', { method: 'POST' })
  const data = (await res.json()) as Record<string, unknown>
  if (!res.ok) throw new Error(String(data.error || 'error al enriquecer'))
  return data
}

/** Aggregated catalog (library + FM + likes). Preferred over raw JSON merge. */
export async function loadCatalog(): Promise<CatalogPayload | null> {
  try {
    const res = await fetch('/api/catalog/full')
    if (res.ok) return (await res.json()) as CatalogPayload
  } catch {
    /* API down */
  }
  return null
}
