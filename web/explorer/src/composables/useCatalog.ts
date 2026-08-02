import { computed, ref } from 'vue'
import * as api from '../api/client'
import type {
  BannerType,
  CatalogAlbum,
  CatalogArtist,
  CatalogSummary,
  CatalogTrack,
  DedupePreview,
  DiskUsage,
  FmSession,
} from '../types'
import { formatBytes } from '../utils/format'
import { transformFmSessions } from '../utils/transform'

export type AppView =
  | 'home'
  | 'search'
  | 'library'
  | 'likes'
  | 'fm'
  | 'admin'
  | 'artist'
  | 'album'

export type LibrarySection =
  | 'all'
  | 'artists'
  | 'albums'
  | 'tracks'
  | 'requests'

export function useCatalog() {
  const loading = ref(true)
  const view = ref<AppView>('home')
  const librarySection = ref<LibrarySection>('artists')
  const filterText = ref('')
  const artistKey = ref<string | null>(null)
  const albumKey = ref<string | null>(null)
  const mobileNavOpen = ref(false)

  const tracks = ref<CatalogTrack[]>([])
  const artists = ref<CatalogArtist[]>([])
  const albums = ref<CatalogAlbum[]>([])
  const summary = ref<CatalogSummary | null>(null)
  const fmSessions = ref<FmSession[]>([])
  const selectedFmSessionId = ref<string | null>(null)
  const diskUsage = ref<DiskUsage>({
    total_bytes: 0,
    files: {},
    tracks_on_disk: 0,
  })
  const dedupePreview = ref<DedupePreview | null>(null)
  const enrichSuggest = ref<number | null>(null)
  const busyDedupe = ref(false)
  const busyEnrich = ref(false)
  const banner = ref<{ msg: string; type: BannerType } | null>(null)
  const cacheDir = ref<string | null>(null)

  function showBanner(msg: string, type: BannerType = 'info') {
    banner.value = { msg, type }
  }
  function clearBanner() {
    banner.value = null
  }

  const q = computed(() => filterText.value.trim().toLowerCase())

  function matches(blob: string): boolean {
    if (!q.value) return true
    return blob.toLowerCase().includes(q.value)
  }

  const filteredTracks = computed(() => {
    let list = tracks.value
    if (view.value === 'library' && librarySection.value === 'requests') {
      list = list.filter(
        (t) => (t.request_count || 0) > 0 || t.sources?.includes('request'),
      )
    }
    if (!q.value) return list
    return list.filter((t) =>
      matches(`${t.title} ${t.artist} ${t.album} ${t.yt_query || ''}`),
    )
  })

  const filteredArtists = computed(() => {
    if (!q.value) return artists.value
    return artists.value.filter((a) => matches(a.name))
  })

  const filteredAlbums = computed(() => {
    if (!q.value) return albums.value
    return albums.value.filter((a) => matches(`${a.name} ${a.artist}`))
  })

  const recentRequests = computed(() =>
    [...tracks.value]
      .filter((t) => (t.request_count || 0) > 0)
      .sort(
        (a, b) =>
          (b.last_requested || b.cached_at || 0) -
          (a.last_requested || a.cached_at || 0),
      )
      .slice(0, 12),
  )

  const recentFm = computed(() =>
    [...tracks.value]
      .filter((t) => t.source === 'fm' || t.sources?.includes('fm'))
      .sort((a, b) => (b.last_played || 0) - (a.last_played || 0))
      .slice(0, 12),
  )

  const topPlayed = computed(() =>
    [...tracks.value]
      .filter((t) => t.on_disk)
      .sort((a, b) => (b.play_count || 0) - (a.play_count || 0))
      .slice(0, 12),
  )

  const currentArtist = computed(() => {
    if (!artistKey.value) return null
    const a = artists.value.find((x) => x.key === artistKey.value)
    if (!a) return null
    const atr = tracks.value.filter((t) => t.artist_key === artistKey.value)
    const aalb = albums.value.filter((x) => x.artist_key === artistKey.value)
    return { ...a, tracks: atr, albums: aalb }
  })

  const currentAlbum = computed(() => {
    if (!artistKey.value || !albumKey.value) return null
    const full = albumKey.value.includes('::')
      ? albumKey.value
      : `${artistKey.value}::${albumKey.value}`
    const al =
      albums.value.find((x) => x.key === full) ||
      albums.value.find(
        (x) =>
          x.artist_key === artistKey.value &&
          (x.key.endsWith(`::${albumKey.value}`) || x.name === albumKey.value),
      )
    if (!al) return null
    const atr = tracks.value
      .filter(
        (t) =>
          t.artist_key === al.artist_key &&
          (t.album_key === al.key.split('::').pop() ||
            t.album_display === al.name),
      )
      .sort((a, b) => a.title.localeCompare(b.title, 'es'))
    return { ...al, tracks: atr }
  })

  function setView(v: AppView) {
    view.value = v
    mobileNavOpen.value = false
    if (v !== 'artist' && v !== 'album') {
      // keep keys when navigating within detail; clear when leaving music tree via nav
    }
  }

  function goHome() {
    artistKey.value = null
    albumKey.value = null
    view.value = 'home'
    mobileNavOpen.value = false
  }

  function goLibrary(section: LibrarySection = 'artists') {
    librarySection.value = section
    artistKey.value = null
    albumKey.value = null
    view.value = 'library'
    mobileNavOpen.value = false
  }

  function openArtist(key: string) {
    artistKey.value = key
    albumKey.value = null
    view.value = 'artist'
    mobileNavOpen.value = false
  }

  function openAlbum(artist: string, album: string) {
    artistKey.value = artist
    albumKey.value = album.includes('::') ? album.split('::').pop()! : album
    view.value = 'album'
    mobileNavOpen.value = false
  }

  function breadcrumb(): { label: string; action?: () => void }[] {
    const crumbs: { label: string; action?: () => void }[] = []
    if (view.value === 'home') return [{ label: 'Inicio' }]
    if (view.value === 'search') return [{ label: 'Buscar' }]
    if (view.value === 'likes') return [{ label: 'Me gusta' }]
    if (view.value === 'fm') return [{ label: 'Radio FM' }]
    if (view.value === 'admin') return [{ label: 'Admin' }]
    if (view.value === 'library') {
      crumbs.push({ label: 'Biblioteca', action: () => goLibrary('artists') })
      const sec: Record<LibrarySection, string> = {
        all: 'Todo',
        artists: 'Artistas',
        albums: 'Álbumes',
        tracks: 'Canciones',
        requests: 'Pedidos',
      }
      crumbs.push({ label: sec[librarySection.value] })
      return crumbs
    }
    if (view.value === 'artist' && currentArtist.value) {
      crumbs.push({ label: 'Biblioteca', action: () => goLibrary('artists') })
      crumbs.push({ label: 'Artistas', action: () => goLibrary('artists') })
      crumbs.push({ label: currentArtist.value.name })
      return crumbs
    }
    if (view.value === 'album' && currentAlbum.value) {
      crumbs.push({ label: 'Biblioteca', action: () => goLibrary('albums') })
      crumbs.push({
        label: currentAlbum.value.artist,
        action: () => openArtist(currentAlbum.value!.artist_key),
      })
      crumbs.push({ label: currentAlbum.value.name })
      return crumbs
    }
    return [{ label: 'sona' }]
  }

  async function reload() {
    const [catalog, fmRaw, disk, dedupe] = await Promise.all([
      api.loadCatalog(),
      api.loadCacheJson('fm_sessions.json'),
      api.loadDiskUsage(),
      api.loadDedupePreview(),
    ])
    cacheDir.value = api.getCacheDir()
    diskUsage.value = disk
    dedupePreview.value = dedupe
    fmSessions.value = transformFmSessions(fmRaw)

    if (catalog) {
      tracks.value = catalog.tracks || []
      artists.value = catalog.artists || []
      albums.value = catalog.albums || []
      summary.value = catalog.summary || null
    } else {
      tracks.value = []
      artists.value = []
      albums.value = []
      summary.value = null
      showBanner(
        'Catálogo no disponible — ¿explorer API en marcha?',
        'error',
      )
    }

    try {
      const pre = await api.enrichPreview()
      enrichSuggest.value =
        typeof pre.suggest_enrich === 'number' ? pre.suggest_enrich : null
    } catch {
      /* optional */
    }
  }

  async function init() {
    loading.value = true
    try {
      await reload()
    } finally {
      loading.value = false
    }
  }

  async function doDedupe() {
    if (!dedupePreview.value?.wasted_bytes) return
    busyDedupe.value = true
    try {
      const data = await api.runDedupe()
      showBanner(
        `liberados ${formatBytes(Number(data.bytes_freed) || 0)}`,
        'success',
      )
      await reload()
    } catch (e) {
      showBanner(String(e), 'error')
    } finally {
      busyDedupe.value = false
    }
  }

  async function doEnrich() {
    busyEnrich.value = true
    try {
      const data = await api.runEnrich()
      showBanner(
        `enriquecidas: ${data.updated || 0} (de ${data.processed || 0})`,
        'success',
      )
      await reload()
    } catch (e) {
      showBanner(String(e), 'error')
    } finally {
      busyEnrich.value = false
    }
  }

  async function deleteTrack(trackId: string) {
    if (!trackId || trackId.startsWith('fm_')) {
      showBanner('captura FM: solo lectura', 'info')
      return
    }
    try {
      const { ok, data, status } = await api.deleteLibraryTrack(trackId)
      if (!ok) {
        showBanner(String(data.error || status), 'error')
        return
      }
      showBanner('eliminado', 'success')
      await reload()
    } catch (e) {
      showBanner(String(e), 'error')
    }
  }

  const filteredFm = computed(() =>
    fmSessions.value.filter((s) =>
      matches(
        `${s.station_name} ${s.tags} ${s.countrycode} ${s.stationuuid}`,
      ),
    ),
  )

  return {
    loading,
    view,
    librarySection,
    filterText,
    artistKey,
    albumKey,
    mobileNavOpen,
    tracks,
    artists,
    albums,
    summary,
    fmSessions,
    selectedFmSessionId,
    diskUsage,
    dedupePreview,
    enrichSuggest,
    busyDedupe,
    busyEnrich,
    banner,
    cacheDir,
    filteredTracks,
    filteredArtists,
    filteredAlbums,
    recentRequests,
    recentFm,
    topPlayed,
    currentArtist,
    currentAlbum,
    filteredFm,
    showBanner,
    clearBanner,
    setView,
    goHome,
    goLibrary,
    openArtist,
    openAlbum,
    breadcrumb,
    init,
    reload,
    doDedupe,
    doEnrich,
    deleteTrack,
  }
}
