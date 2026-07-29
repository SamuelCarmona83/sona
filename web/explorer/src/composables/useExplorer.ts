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
  StatTile,
  TabId,
  TableSort,
  ViewMode,
} from '../types'
import { formatBytes } from '../utils/format'
import { isLibraryOutlier } from '../utils/outliers'
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

export interface ConfirmRequest {
  title: string
  body: string
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
}

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
  const secondaryLoading = ref(false)
  const gridLimit = ref({
    searches: GRID_INITIAL_LIMIT,
    library: GRID_INITIAL_LIMIT,
    likes: GRID_INITIAL_LIMIT,
  })

  const banner = ref<{ msg: string; type: BannerType } | null>(null)
  const busyDedupe = ref(false)
  const busyEnrich = ref(false)
  const showOutliersOnly = ref(false)
  const enrichSuggest = ref<number | null>(null)
  const confirmDialog = ref<(ConfirmRequest & { busy?: boolean }) | null>(
    null,
  )
  let confirmResolve: ((ok: boolean) => void) | null = null

  function showBanner(msg: string, type: BannerType = 'info') {
    banner.value = { msg, type }
  }

  function clearBanner() {
    banner.value = null
  }

  function askConfirm(req: ConfirmRequest): Promise<boolean> {
    return new Promise((resolve) => {
      confirmResolve = resolve
      confirmDialog.value = { ...req, busy: false }
    })
  }

  function resolveConfirm(ok: boolean) {
    const resolve = confirmResolve
    confirmResolve = null
    confirmDialog.value = null
    resolve?.(ok)
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

  const outlierCount = computed(
    () => library.value.filter((i) => isLibraryOutlier(i)).length,
  )

  const filteredLibrary = computed(() => {
    let items = library.value.filter((i) =>
      matchesFilter([i.title, i.artist, i.yt_query, i.trackId]),
    )
    if (showOutliersOnly.value) {
      items = items.filter((i) => isLibraryOutlier(i))
    }
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

  const statsTiles = computed((): StatTile[] => {
    if (loading.value) return []
    const fmTracks = fmSessions.value.reduce(
      (n, s) => n + (s.track_count || 0),
      0,
    )
    const tiles: StatTile[] = [
      { value: String(library.value.length), label: 'biblioteca' },
      { value: String(searches.value.length), label: 'búsquedas' },
      { value: String(likes.value.length), label: 'likes' },
      {
        value:
          fmTracks > 0
            ? `${fmSessions.value.length}/${fmTracks}`
            : String(fmSessions.value.length),
        label: fmTracks > 0 ? 'fm / det.' : 'fm',
      },
      {
        value: formatBytes(diskUsage.value.total_bytes),
        label: 'disco',
      },
    ]
    if (dedupePreview.value && dedupePreview.value.wasted_bytes > 0) {
      tiles.push({
        value: String(dedupePreview.value.duplicate_groups),
        label: `dup · ${formatBytes(dedupePreview.value.wasted_bytes)}`,
        warn: true,
      })
    }
    return tiles
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
    const ok = await askConfirm({
      title: '¿eliminar de la biblioteca?',
      body:
        `${title}\n` +
        `id: ${trackId}\n` +
        `tamaño: ${size}\n\n` +
        'borra el archivo y la entrada del índice. no se puede deshacer.',
      confirmLabel: 'eliminar',
      danger: true,
    })
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
      void refreshEnrichSuggest()
      showBanner(
        `eliminado · ${formatBytes(Number(data.bytes_freed) || 0)} liberados · ${trackId}`,
        'success',
      )
    } catch (err) {
      showBanner(`error al eliminar: ${err}`, 'error')
    }
  }

  async function doDedupe() {
    if (!dedupePreview.value || dedupePreview.value.wasted_bytes <= 0) return
    const ok = await askConfirm({
      title: '¿eliminar duplicados?',
      body:
        `recuperar ~${formatBytes(dedupePreview.value.wasted_bytes)}\n\n` +
        `${dedupePreview.value.duplicate_groups} grupos · ${dedupePreview.value.files_to_delete?.length || 0} archivos\n\n` +
        'recomendado: detener el bot antes (docker compose stop bot).',
      confirmLabel: 'dedupe',
      danger: true,
    })
    if (!ok) return

    busyDedupe.value = true
    try {
      const data = await api.runDedupe()
      showBanner(
        `liberados ${formatBytes(Number(data.bytes_freed) || dedupePreview.value.wasted_bytes)} · ${data.entries_after} entradas`,
        'success',
      )
      await reloadData()
      void refreshEnrichSuggest()
    } catch (err) {
      showBanner(
        `[-] error: ${err instanceof Error ? err.message : err}`,
        'error',
      )
    } finally {
      busyDedupe.value = false
    }
  }

  async function refreshEnrichSuggest() {
    try {
      const pre = await api.enrichPreview()
      enrichSuggest.value =
        typeof pre.suggest_enrich === 'number' ? pre.suggest_enrich : null
    } catch {
      /* API optional */
    }
  }

  async function doEnrich() {
    let suggest = enrichSuggest.value
    if (suggest == null) {
      try {
        const pre = await api.enrichPreview()
        suggest = pre.suggest_enrich || 0
        enrichSuggest.value = suggest
      } catch {
        suggest = 0
      }
    }

    if (suggest <= 0) {
      const ok = await askConfirm({
        title: '¿enriquecer de todas formas?',
        body: 'no hay muchas entradas pendientes de enriquecer.',
        confirmLabel: 'enriquecer',
      })
      if (!ok) return
    } else {
      const ok = await askConfirm({
        title: '¿enriquecer biblioteca?',
        body: `se intentará enriquecer ~${suggest} entradas (cover / metadata).`,
        confirmLabel: `enriquecer · ${suggest}`,
      })
      if (!ok) return
    }

    busyEnrich.value = true
    try {
      const data = await api.runEnrich()
      showBanner(
        `enriquecidas: ${data.updated || 0} (de ${data.processed || 0})`,
        'success',
      )
      await reloadData()
      void refreshEnrichSuggest()
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
    secondaryLoading.value = false
  }

  async function loadSecondaryData() {
    if (secondaryDataLoaded.value || secondaryLoading.value) return
    secondaryLoading.value = true
    try {
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
    } finally {
      secondaryLoading.value = false
    }
  }

  const tabBusy = computed(() => {
    if (loading.value) return false
    if (secondaryDataLoaded.value) return false
    return (
      secondaryLoading.value &&
      (activeTab.value === 'likes' || activeTab.value === 'fm')
    )
  })

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
    void refreshEnrichSuggest()

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
    showOutliersOnly,
    outlierCount,
    enrichSuggest,
    confirmDialog,
    secondaryDataLoaded,
    secondaryLoading,
    tabBusy,
    filteredSearches,
    filteredLibrary,
    filteredLikes,
    filteredFm,
    statsTiles,
    showBanner,
    clearBanner,
    resolveConfirm,
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
