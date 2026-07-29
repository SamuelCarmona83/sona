import { computed, ref } from 'vue'
import * as api from '../api/client'
import type {
  BannerType,
  DedupePreview,
  DiskUsage,
  FmSession,
  LibraryItem,
  LikeItem,
  SearchItem,
  SortKey,
  TabId,
  TableSort,
  ViewMode,
} from '../types'
import { formatBytes } from '../utils/format'
import {
  buildTransitions,
  countPlayedIds,
  groupLibraryItems,
  transformFmSessions,
  transformLibrary,
  transformLikes,
  transformSearches,
} from '../utils/transform'
import { DEFAULT_TABLE_SORT, GRID_INITIAL_LIMIT } from '../ui'

export function useExplorer() {
  const loading = ref(true)
  const cacheDir = ref<string | null>(null)
  const activeTab = ref<TabId>('searches')
  const viewMode = ref<ViewMode>('grid')
  const sortKey = ref<SortKey>('recent')
  const filterText = ref('')
  const tableSort = ref<TableSort>({ ...DEFAULT_TABLE_SORT.searches })
  const libraryGrouped = ref(true)

  const searches = ref<SearchItem[]>([])
  const library = ref<LibraryItem[]>([])
  const likes = ref<LikeItem[]>([])
  const fmSessions = ref<FmSession[]>([])
  const selectedFmSessionId = ref<string | null>(null)
  const playedCount = ref(0)
  const diskUsage = ref<DiskUsage>({
    total_bytes: 0,
    files: {},
    tracks_on_disk: 0,
  })
  const dedupePreview = ref<DedupePreview | null>(null)
  const secondaryDataLoaded = ref(false)
  const gridLimit = ref({
    searches: GRID_INITIAL_LIMIT,
    library: GRID_INITIAL_LIMIT,
    likes: GRID_INITIAL_LIMIT,
  })

  const banner = ref<{ msg: string; type: BannerType } | null>(null)
  const busyDedupe = ref(false)
  const busyEnrich = ref(false)

  function showBanner(msg: string, type: BannerType = 'info') {
    banner.value = { msg, type }
  }

  function matchesFilter(fields: unknown[]): boolean {
    const q = filterText.value.trim().toLowerCase()
    if (!q) return true
    return fields.some((f) => String(f || '').toLowerCase().includes(q))
  }

  function emptyMessage(tab: TabId, hasFilter: boolean): string {
    if (hasFilter) return 'sin resultados'
    if (tab === 'library') return 'biblioteca vacía'
    if (tab === 'likes') return 'sin likes'
    if (tab === 'fm') return 'sin sesiones FM — escuchá con !fm en discord'
    return 'sin búsquedas'
  }

  const filteredSearches = computed(() => {
    const items = searches.value.filter((i) =>
      matchesFilter([i.query, i.title, i.uploader]),
    )
    return sortItems(items, 'searches')
  })

  const filteredLibrary = computed(() => {
    let items = library.value.filter((i) =>
      matchesFilter([i.title, i.artist, i.yt_query, i.trackId]),
    )
    if (libraryGrouped.value && viewMode.value === 'table') {
      items = groupLibraryItems(items)
    }
    return sortItems(items, 'library')
  })

  const filteredLikes = computed(() => {
    const items = likes.value.filter((i) =>
      matchesFilter([i.title, i.artist, i.yt_query]),
    )
    return sortItems(items, 'likes')
  })

  const filteredFm = computed(() =>
    fmSessions.value.filter((i) =>
      matchesFilter([i.station_name, i.tags, i.countrycode, i.stationuuid]),
    ),
  )

  function field(item: object, key: string): unknown {
    return (item as Record<string, unknown>)[key]
  }

  function sortItems<T extends object>(items: T[], tab: TabId): T[] {
    if (viewMode.value === 'table' && tab !== 'fm') {
      return sortByTable(items)
    }
    return sortByDropdown(items, tab)
  }

  function sortByDropdown<T extends object>(items: T[], tab: TabId): T[] {
    const key = sortKey.value
    const sorted = [...items]
    if (tab === 'searches') {
      if (key === 'alpha')
        sorted.sort((a, b) =>
          String(field(a, 'title') || '').localeCompare(
            String(field(b, 'title') || ''),
          ),
        )
      else if (key === 'duration')
        sorted.sort(
          (a, b) =>
            Number(field(b, 'duration') || 0) -
            Number(field(a, 'duration') || 0),
        )
      else
        sorted.sort(
          (a, b) =>
            Number(field(b, 'cached_at') || 0) -
            Number(field(a, 'cached_at') || 0),
        )
    } else if (tab === 'library') {
      if (key === 'alpha')
        sorted.sort((a, b) =>
          String(field(a, 'title') || '').localeCompare(
            String(field(b, 'title') || ''),
          ),
        )
      else if (key === 'duration')
        sorted.sort(
          (a, b) =>
            Number(field(b, 'duration') || 0) -
            Number(field(a, 'duration') || 0),
        )
      else if (key === 'size')
        sorted.sort(
          (a, b) =>
            Number(field(b, 'file_size_bytes') || 0) -
            Number(field(a, 'file_size_bytes') || 0),
        )
      else
        sorted.sort(
          (a, b) =>
            Number(field(b, 'play_count') || 0) -
            Number(field(a, 'play_count') || 0),
        )
    } else if (tab === 'likes') {
      if (key === 'alpha')
        sorted.sort((a, b) =>
          String(field(a, 'title') || '').localeCompare(
            String(field(b, 'title') || ''),
          ),
        )
      else
        sorted.sort(
          (a, b) =>
            Number(field(b, 'liked_at') || 0) -
            Number(field(a, 'liked_at') || 0),
        )
    }
    return sorted
  }

  function sortByTable<T extends object>(items: T[]): T[] {
    const { column, dir } = tableSort.value
    const sorted = [...items]
    const mul = dir === 'asc' ? 1 : -1
    sorted.sort((a, b) => {
      let av = field(a, column) as string | number | boolean | undefined
      let bv = field(b, column) as string | number | boolean | undefined
      if (typeof av === 'boolean') av = av ? 1 : 0
      if (typeof bv === 'boolean') bv = bv ? 1 : 0
      if (typeof av === 'number' && typeof bv === 'number')
        return (av - bv) * mul
      return String(av || '').localeCompare(String(bv || ''), 'es') * mul
    })
    return sorted
  }

  const statsParts = computed(() => {
    const fmTracks = fmSessions.value.reduce(
      (n, s) => n + (s.track_count || 0),
      0,
    )
    const parts: { text: string; warn?: boolean }[] = [
      { text: `${library.value.length} biblioteca` },
      { text: `${searches.value.length} búsquedas` },
      { text: `${likes.value.length} likes` },
      { text: `${fmSessions.value.length} fm` },
    ]
    if (fmTracks > 0) parts.push({ text: `${fmTracks} detecciones` })
    parts.push({ text: `${formatBytes(diskUsage.value.total_bytes)} disco` })
    if (dedupePreview.value && dedupePreview.value.wasted_bytes > 0) {
      parts.push({
        text: `${dedupePreview.value.duplicate_groups} dup`,
        warn: true,
      })
    }
    return parts
  })

  function setTab(tab: TabId) {
    activeTab.value = tab
    if (viewMode.value === 'table' && tab !== 'fm') {
      tableSort.value = {
        ...(DEFAULT_TABLE_SORT[tab] || DEFAULT_TABLE_SORT.searches),
      }
    }
    if (!secondaryDataLoaded.value && (tab === 'likes' || tab === 'fm')) {
      void loadSecondaryData()
    }
  }

  function setViewMode(mode: ViewMode) {
    viewMode.value = mode
    if (mode === 'table' && activeTab.value !== 'fm') {
      tableSort.value = {
        ...(DEFAULT_TABLE_SORT[activeTab.value] || DEFAULT_TABLE_SORT.searches),
      }
    }
  }

  function setLibraryGrouped(grouped: boolean) {
    libraryGrouped.value = grouped
    if (viewMode.value === 'table') {
      tableSort.value = { ...DEFAULT_TABLE_SORT.library }
    }
  }

  function onFilterInput(value: string) {
    filterText.value = value
    gridLimit.value = {
      searches: GRID_INITIAL_LIMIT,
      library: GRID_INITIAL_LIMIT,
      likes: GRID_INITIAL_LIMIT,
    }
  }

  function showMore(tab: 'searches' | 'library' | 'likes') {
    gridLimit.value = {
      ...gridLimit.value,
      [tab]: (gridLimit.value[tab] || GRID_INITIAL_LIMIT) + GRID_INITIAL_LIMIT,
    }
  }

  function toggleTableSort(column: string) {
    if (!column || column === 'actions') return
    if (tableSort.value.column === column) {
      tableSort.value = {
        column,
        dir: tableSort.value.dir === 'asc' ? 'desc' : 'asc',
      }
    } else {
      tableSort.value = { column, dir: 'desc' }
    }
  }

  function transitionsForStation(stationuuid: string) {
    const tracks = []
    for (const s of fmSessions.value) {
      if (stationuuid && s.stationuuid !== stationuuid) continue
      for (const t of s.tracks || []) tracks.push(t)
    }
    return buildTransitions(tracks)
  }

  async function deleteTrack(trackId: string) {
    if (!trackId) return
    const item = library.value.find((i) => i.trackId === trackId)
    const title = item?.title || trackId
    const size = item?.on_disk ? formatBytes(item.file_size_bytes) : '—'
    const ok = confirm(
      `¿eliminar de la biblioteca?\n\n` +
        `${title}\n` +
        `id: ${trackId}\n` +
        `tamaño: ${size}\n\n` +
        `borra el archivo y la entrada del índice. no se puede deshacer.`,
    )
    if (!ok) return

    try {
      const { ok: resOk, status, data } = await api.deleteLibraryTrack(trackId)
      if (!resOk) {
        showBanner(
          String(data.error || `error al eliminar (${status})`),
          'error',
        )
        return
      }
      library.value = library.value.filter((i) => i.trackId !== trackId)
      if (diskUsage.value.files && diskUsage.value.files[trackId] != null) {
        diskUsage.value = {
          ...diskUsage.value,
          total_bytes: Math.max(
            0,
            (diskUsage.value.total_bytes || 0) -
              (diskUsage.value.files[trackId] || 0),
          ),
          tracks_on_disk: Math.max(
            0,
            (diskUsage.value.tracks_on_disk || 1) - 1,
          ),
          files: Object.fromEntries(
            Object.entries(diskUsage.value.files).filter(
              ([k]) => k !== trackId,
            ),
          ),
        }
      }
      void api.loadDiskUsage().then((disk) => {
        diskUsage.value = disk
      })
      showBanner(
        `eliminado · ${formatBytes(Number(data.bytes_freed) || 0)} liberados · ${trackId}`,
        'info',
      )
    } catch (err) {
      showBanner(`error al eliminar: ${err}`, 'error')
    }
  }

  async function doDedupe() {
    if (!dedupePreview.value || dedupePreview.value.wasted_bytes <= 0) return
    const msg =
      `¿eliminar duplicados y recuperar ~${formatBytes(dedupePreview.value.wasted_bytes)}?\n\n` +
      `${dedupePreview.value.duplicate_groups} grupos · ${dedupePreview.value.files_to_delete?.length || 0} archivos\n\n` +
      'recomendado: detener el bot antes (docker compose stop bot).'
    if (!confirm(msg)) return

    busyDedupe.value = true
    try {
      const data = await api.runDedupe()
      showBanner(
        `liberados ${formatBytes(Number(data.bytes_freed) || dedupePreview.value.wasted_bytes)} · ${data.entries_after} entradas`,
        'info',
      )
      await reloadData()
    } catch (err) {
      showBanner(
        `[-] error: ${err instanceof Error ? err.message : err}`,
        'error',
      )
    } finally {
      busyDedupe.value = false
    }
  }

  async function doEnrich() {
    busyEnrich.value = true
    try {
      const pre = await api.enrichPreview()
      const suggest = pre.suggest_enrich || 0
      if (
        suggest <= 0 &&
        !confirm('No hay muchas entradas por enriquecer. ¿Ejecutar de todas formas?')
      ) {
        return
      }
      const data = await api.runEnrich()
      showBanner(
        `enriquecidas: ${data.updated || 0} (de ${data.processed || 0})`,
        'info',
      )
      await reloadData()
    } catch (err) {
      showBanner(
        `[-] error enriquecer: ${err instanceof Error ? err.message : err}`,
        'error',
      )
    } finally {
      busyEnrich.value = false
    }
  }

  async function reloadData() {
    const [libIndex, fmRaw, disk, dedupe] = await Promise.all([
      api.loadCacheJson('library_index.json'),
      api.loadCacheJson('fm_sessions.json'),
      api.loadDiskUsage(),
      api.loadDedupePreview(),
    ])
    diskUsage.value = disk
    dedupePreview.value = dedupe
    library.value = transformLibrary(libIndex, disk)
    fmSessions.value = transformFmSessions(fmRaw)
    secondaryDataLoaded.value = true
  }

  async function loadSecondaryData() {
    if (secondaryDataLoaded.value) return
    const [likesRaw, playedRaw, fmRaw, libIndex] = await Promise.all([
      api.loadCacheJson('likes.json'),
      api.loadCacheJson('played_ids.json'),
      api.loadCacheJson('fm_sessions.json'),
      library.value.length
        ? Promise.resolve(null)
        : api.loadCacheJson('library_index.json'),
    ])
    likes.value = transformLikes(likesRaw)
    fmSessions.value = transformFmSessions(fmRaw)
    playedCount.value = countPlayedIds(playedRaw)
    if (libIndex) library.value = transformLibrary(libIndex, diskUsage.value)
    secondaryDataLoaded.value = true
    void api.loadDedupePreview().then((d) => {
      dedupePreview.value = d
    })
  }

  async function init() {
    loading.value = true
    const [ytMeta, libIndex, disk] = await Promise.all([
      api.loadCacheJson('youtube_metadata.json'),
      api.loadCacheJson('library_index.json'),
      api.loadDiskUsage(),
    ])

    cacheDir.value = api.getCacheDir()
    if (!cacheDir.value) {
      showBanner('sin caché — docker compose up -d explorer', 'error')
      loading.value = false
      return
    }

    diskUsage.value = disk
    searches.value = transformSearches(ytMeta)
    library.value = transformLibrary(libIndex, disk)
    loading.value = false

    const idle =
      window.requestIdleCallback ||
      ((fn: () => void) => setTimeout(fn, 50) as unknown as number)
    idle(() => {
      void loadSecondaryData().catch((err) =>
        console.warn('secondary load', err),
      )
    })

    if (searches.value.length === 0 && library.value.length === 0) {
      showBanner('caché vacía — usa !play / !fm en discord', 'info')
    }
  }

  return {
    loading,
    cacheDir,
    activeTab,
    viewMode,
    sortKey,
    filterText,
    tableSort,
    libraryGrouped,
    searches,
    library,
    likes,
    fmSessions,
    selectedFmSessionId,
    diskUsage,
    dedupePreview,
    gridLimit,
    banner,
    busyDedupe,
    busyEnrich,
    filteredSearches,
    filteredLibrary,
    filteredLikes,
    filteredFm,
    statsParts,
    showBanner,
    emptyMessage,
    setTab,
    setViewMode,
    setLibraryGrouped,
    onFilterInput,
    showMore,
    toggleTableSort,
    transitionsForStation,
    buildTransitions,
    deleteTrack,
    doDedupe,
    doEnrich,
    reloadData,
    init,
  }
}
