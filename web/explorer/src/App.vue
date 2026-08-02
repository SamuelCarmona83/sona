<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import Banner from './components/shell/Banner.vue'
import LoadingBlock from './components/chrome/LoadingBlock.vue'
import EmptyState from './components/chrome/EmptyState.vue'
import FmSessionPanel from './components/FmSessionPanel.vue'
import CoverCard from './components/music/CoverCard.vue'
import TrackRow from './components/music/TrackRow.vue'
import {
  type AppView,
  type LibrarySection,
  useCatalog,
} from './composables/useCatalog'
import { formatBytes } from './utils/format'
import { UI } from './ui'
import { buildTransitions } from './utils/transform'

const cat = useCatalog()

const navMain: { id: AppView; label: string }[] = [
  { id: 'home', label: 'Inicio' },
  { id: 'search', label: 'Buscar' },
  { id: 'library', label: 'Biblioteca' },
  { id: 'likes', label: 'Me gusta' },
  { id: 'fm', label: 'Radio FM' },
]

const libSections: { id: LibrarySection; label: string }[] = [
  { id: 'artists', label: 'Artistas' },
  { id: 'albums', label: 'Álbumes' },
  { id: 'tracks', label: 'Canciones' },
  { id: 'requests', label: 'Pedidos' },
  { id: 'all', label: 'Todo' },
]

const pageTitle = computed(() => {
  if (cat.view.value === 'artist' && cat.currentArtist.value)
    return cat.currentArtist.value.name
  if (cat.view.value === 'album' && cat.currentAlbum.value)
    return cat.currentAlbum.value.name
  if (cat.view.value === 'library') {
    const m: Record<LibrarySection, string> = {
      artists: 'Artistas',
      albums: 'Álbumes',
      tracks: 'Canciones',
      requests: 'Pedidos',
      all: 'Biblioteca',
    }
    return m[cat.librarySection.value]
  }
  const titles: Partial<Record<AppView, string>> = {
    home: 'Inicio',
    search: 'Buscar',
    likes: 'Me gusta',
    fm: 'Radio FM',
    admin: 'Admin',
  }
  return titles[cat.view.value] || 'sona'
})

const likedTracks = computed(() =>
  cat.tracks.value.filter((t) => t.liked),
)

function onNav(id: AppView) {
  if (id === 'library') cat.goLibrary(cat.librarySection.value || 'artists')
  else if (id === 'home') cat.goHome()
  else {
    cat.artistKey.value = null
    cat.albumKey.value = null
    cat.setView(id)
  }
}

function applyUrlOnce() {
  const p = new URLSearchParams(window.location.search)
  const view = p.get('view') as AppView | null
  const section = p.get('section') as LibrarySection | null
  const artist = p.get('artist')
  const album = p.get('album')
  const q = p.get('q')
  if (q) cat.filterText.value = q
  if (view === 'artist' && artist) {
    cat.openArtist(artist)
    return
  }
  if (view === 'album' && artist && album) {
    cat.openAlbum(artist, album)
    return
  }
  if (view === 'library') {
    cat.goLibrary(section || 'artists')
    return
  }
  if (
    view &&
    ['home', 'search', 'likes', 'fm', 'admin'].includes(view)
  ) {
    cat.setView(view)
  }
}

function syncUrl() {
  const params = new URLSearchParams()
  const v = cat.view.value
  if (v !== 'home') params.set('view', v)
  if (v === 'library' && cat.librarySection.value !== 'artists') {
    params.set('section', cat.librarySection.value)
  }
  if (cat.artistKey.value) params.set('artist', cat.artistKey.value)
  if (cat.albumKey.value && v === 'album')
    params.set('album', cat.albumKey.value)
  if (cat.filterText.value.trim())
    params.set('q', cat.filterText.value.trim())
  const qs = params.toString()
  const next = qs
    ? `${window.location.pathname}?${qs}`
    : window.location.pathname
  if (next !== `${window.location.pathname}${window.location.search}`) {
    window.history.replaceState(null, '', next)
  }
}

watch(
  [
    cat.view,
    cat.librarySection,
    cat.artistKey,
    cat.albumKey,
    cat.filterText,
  ],
  () => syncUrl(),
)

onMounted(() => {
  applyUrlOnce()
  void cat.init()
})

function transitionsForStation(stationuuid: string) {
  const tracks = []
  for (const s of cat.fmSessions.value) {
    if (stationuuid && s.stationuuid !== stationuuid) continue
    for (const t of s.tracks || []) tracks.push(t)
  }
  return buildTransitions(tracks)
}
</script>

<template>
  <div class="min-h-full bg-canvas text-body font-mono flex">
    <!-- Sidebar -->
    <aside
      class="hidden md:flex w-56 shrink-0 flex-col border-r border-black/10 bg-soft min-h-screen sticky top-0 h-screen"
    >
      <div class="px-5 py-6 border-b border-black/10">
        <p class="text-[10px] text-ash tracking-wide mb-1">sona</p>
        <h1 class="text-lg font-bold text-ink m-0 tracking-tight">explorer</h1>
      </div>
      <nav class="flex-1 p-3 space-y-0.5" aria-label="Principal">
        <button
          v-for="item in navMain"
          :key="item.id"
          type="button"
          class="w-full text-left px-3 py-2 text-sm rounded-sm transition-colors"
          :class="
            (item.id === 'library'
              ? cat.view.value === 'library' ||
                cat.view.value === 'artist' ||
                cat.view.value === 'album'
              : cat.view.value === item.id)
              ? 'bg-ink text-canvas font-medium'
              : 'text-mute hover:text-ink hover:bg-canvas'
          "
          @click="onNav(item.id)"
        >
          {{ item.label }}
        </button>
        <div
          v-if="
            cat.view.value === 'library' ||
            cat.view.value === 'artist' ||
            cat.view.value === 'album'
          "
          class="pl-2 mt-1 space-y-0.5 border-l border-black/10 ml-3"
        >
          <button
            v-for="sec in libSections"
            :key="sec.id"
            type="button"
            class="w-full text-left px-3 py-1.5 text-[12px] rounded-sm"
            :class="
              cat.view.value === 'library' &&
              cat.librarySection.value === sec.id
                ? 'text-ink font-medium'
                : 'text-ash hover:text-ink'
            "
            @click="cat.goLibrary(sec.id)"
          >
            {{ sec.label }}
          </button>
        </div>
      </nav>
      <div class="p-3 border-t border-black/10">
        <button
          type="button"
          class="w-full text-left px-3 py-2 text-[12px] text-ash hover:text-ink"
          :class="cat.view.value === 'admin' ? 'text-ink font-medium' : ''"
          @click="cat.setView('admin')"
        >
          Admin
        </button>
        <p
          v-if="cat.summary.value"
          class="px-3 pt-2 text-[10px] text-ash leading-relaxed"
        >
          {{ cat.summary.value.tracks }} temas ·
          {{ cat.summary.value.artists }} artistas
        </p>
      </div>
    </aside>

    <!-- Main -->
    <div class="flex-1 min-w-0 flex flex-col min-h-screen">
      <header
        class="sticky top-0 z-20 border-b border-black/10 bg-canvas/95 backdrop-blur-sm"
      >
        <div class="px-4 sm:px-6 py-3 flex flex-wrap items-center gap-3">
          <button
            type="button"
            class="md:hidden text-sm text-mute border border-black/10 px-2 py-1"
            @click="cat.mobileNavOpen.value = !cat.mobileNavOpen.value"
          >
            menú
          </button>
          <nav
            class="flex flex-wrap items-center gap-1 text-[12px] text-ash min-w-0 flex-1"
            aria-label="Miga de pan"
          >
            <template
              v-for="(c, i) in cat.breadcrumb()"
              :key="i"
            >
              <span v-if="i > 0" class="text-ash/60">/</span>
              <button
                v-if="c.action"
                type="button"
                class="hover:text-ink truncate max-w-[140px]"
                @click="c.action()"
              >
                {{ c.label }}
              </button>
              <span v-else class="text-ink font-medium truncate max-w-[200px]">{{
                c.label
              }}</span>
            </template>
          </nav>
          <div class="w-full sm:w-64 sm:ml-auto">
            <input
              id="explorer-filter"
              type="search"
              :class="UI.input"
              placeholder="Buscar en la biblioteca…"
              :value="cat.filterText.value"
              @input="
                cat.filterText.value = ($event.target as HTMLInputElement).value
              "
              @focus="cat.view.value !== 'search' && cat.filterText.value && cat.setView('search')"
            />
          </div>
        </div>
        <!-- mobile nav -->
        <div
          v-if="cat.mobileNavOpen.value"
          class="md:hidden border-t border-black/10 px-3 py-2 flex flex-wrap gap-1 bg-soft"
        >
          <button
            v-for="item in navMain"
            :key="item.id"
            type="button"
            class="px-3 py-1.5 text-[12px] border border-black/10"
            @click="onNav(item.id)"
          >
            {{ item.label }}
          </button>
          <button
            type="button"
            class="px-3 py-1.5 text-[12px] border border-black/10"
            @click="cat.setView('admin')"
          >
            Admin
          </button>
        </div>
      </header>

      <main class="flex-1 px-4 sm:px-6 py-6 max-w-5xl w-full mx-auto">
        <Banner
          v-if="cat.banner.value"
          :message="cat.banner.value.msg"
          :type="cat.banner.value.type"
          @dismiss="cat.clearBanner()"
        />

        <LoadingBlock v-if="cat.loading.value" label="cargando catálogo…" />

        <template v-else>
          <!-- HOME -->
          <section v-if="cat.view.value === 'home'" class="space-y-10">
            <div>
              <h2 class="text-xl font-bold text-ink m-0 mb-1">{{ pageTitle }}</h2>
              <p class="text-[12px] text-mute m-0">
                Pedidos, radio y capturas FM en un solo lugar.
              </p>
            </div>

            <div v-if="cat.recentRequests.value.length">
              <h3 class="text-sm font-bold text-ink mb-3">Pedidos recientes</h3>
              <div
                class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3"
              >
                <CoverCard
                  v-for="t in cat.recentRequests.value"
                  :key="t.id"
                  :title="t.title"
                  :subtitle="t.artist"
                  :cover-url="t.cover_url || t.thumbnail"
                  :meta="`×${t.request_count} pedidos`"
                  @click="cat.openArtist(t.artist_key)"
                />
              </div>
            </div>

            <div v-if="cat.recentFm.value.length">
              <h3 class="text-sm font-bold text-ink mb-3">Detectado en FM</h3>
              <div
                class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3"
              >
                <CoverCard
                  v-for="t in cat.recentFm.value"
                  :key="t.id"
                  :title="t.title"
                  :subtitle="t.artist"
                  :cover-url="t.cover_url || t.thumbnail"
                  :meta="t.station_name || 'fm'"
                  @click="cat.openArtist(t.artist_key)"
                />
              </div>
            </div>

            <div v-if="cat.topPlayed.value.length">
              <h3 class="text-sm font-bold text-ink mb-3">Más reproducido</h3>
              <div class="border border-black/10">
                <TrackRow
                  v-for="(t, i) in cat.topPlayed.value"
                  :key="t.id"
                  :track="t"
                  :index="i + 1"
                  show-album
                  @artist="cat.openArtist"
                  @album="cat.openAlbum"
                  @delete="cat.deleteTrack"
                />
              </div>
            </div>

            <EmptyState
              v-if="
                !cat.recentRequests.value.length &&
                !cat.recentFm.value.length &&
                !cat.topPlayed.value.length
              "
              message="biblioteca vacía"
              hint="en discord: !play · !fm · !like"
            />
          </section>

          <!-- SEARCH -->
          <section v-else-if="cat.view.value === 'search'" class="space-y-6">
            <h2 class="text-xl font-bold text-ink m-0">Buscar</h2>
            <EmptyState
              v-if="!cat.filterText.value.trim()"
              message="escribí en el buscador"
              hint="artistas, álbumes o canciones"
            />
            <template v-else>
              <div v-if="cat.filteredArtists.value.length">
                <h3 class="text-sm font-bold text-ink mb-2">Artistas</h3>
                <div
                  class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3 mb-6"
                >
                  <CoverCard
                    v-for="a in cat.filteredArtists.value.slice(0, 8)"
                    :key="a.key"
                    :title="a.name"
                    :subtitle="`${a.track_count} temas`"
                    :cover-url="a.cover_url"
                    @click="cat.openArtist(a.key)"
                  />
                </div>
              </div>
              <div class="border border-black/10">
                <TrackRow
                  v-for="(t, i) in cat.filteredTracks.value.slice(0, 40)"
                  :key="t.id"
                  :track="t"
                  :index="i + 1"
                  show-album
                  @artist="cat.openArtist"
                  @album="cat.openAlbum"
                  @delete="cat.deleteTrack"
                />
              </div>
              <EmptyState
                v-if="
                  !cat.filteredTracks.value.length &&
                  !cat.filteredArtists.value.length
                "
                message="sin resultados"
                hint="probá otro término"
              />
            </template>
          </section>

          <!-- LIBRARY -->
          <section v-else-if="cat.view.value === 'library'" class="space-y-5">
            <div class="flex flex-wrap items-end justify-between gap-3">
              <h2 class="text-xl font-bold text-ink m-0">{{ pageTitle }}</h2>
              <div class="flex flex-wrap gap-1">
                <button
                  v-for="sec in libSections"
                  :key="sec.id"
                  type="button"
                  class="px-3 py-1 text-[12px] border border-black/10"
                  :class="
                    cat.librarySection.value === sec.id
                      ? 'bg-ink text-canvas'
                      : 'text-mute hover:text-ink'
                  "
                  @click="cat.goLibrary(sec.id)"
                >
                  {{ sec.label }}
                </button>
              </div>
            </div>

            <!-- artists grid -->
            <div
              v-if="
                cat.librarySection.value === 'artists' ||
                cat.librarySection.value === 'all'
              "
            >
              <h3
                v-if="cat.librarySection.value === 'all'"
                class="text-sm font-bold text-ink mb-3"
              >
                Artistas
              </h3>
              <div
                v-if="cat.filteredArtists.value.length"
                class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3"
              >
                <CoverCard
                  v-for="a in cat.filteredArtists.value"
                  :key="a.key"
                  :title="a.name"
                  :subtitle="`${a.album_count} álb. · ${a.track_count} temas`"
                  :cover-url="a.cover_url"
                  @click="cat.openArtist(a.key)"
                />
              </div>
              <EmptyState
                v-else-if="cat.librarySection.value === 'artists'"
                message="sin artistas"
                hint="!play o !fm"
              />
            </div>

            <div
              v-if="
                cat.librarySection.value === 'albums' ||
                cat.librarySection.value === 'all'
              "
              class="mt-6"
            >
              <h3
                v-if="cat.librarySection.value === 'all'"
                class="text-sm font-bold text-ink mb-3"
              >
                Álbumes
              </h3>
              <div
                v-if="cat.filteredAlbums.value.length"
                class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3"
              >
                <CoverCard
                  v-for="a in cat.filteredAlbums.value"
                  :key="a.key"
                  :title="a.name"
                  :subtitle="a.artist"
                  :cover-url="a.cover_url"
                  :meta="`${a.track_count} temas`"
                  @click="cat.openAlbum(a.artist_key, a.key)"
                />
              </div>
              <EmptyState
                v-else-if="cat.librarySection.value === 'albums'"
                message="sin álbumes"
                hint="enrich o metadata de Spotify"
              />
            </div>

            <div
              v-if="
                cat.librarySection.value === 'tracks' ||
                cat.librarySection.value === 'requests' ||
                cat.librarySection.value === 'all'
              "
              class="mt-6"
            >
              <h3
                v-if="cat.librarySection.value === 'all'"
                class="text-sm font-bold text-ink mb-3"
              >
                Canciones
              </h3>
              <div
                v-if="cat.filteredTracks.value.length"
                class="border border-black/10"
              >
                <TrackRow
                  v-for="(t, i) in cat.filteredTracks.value.slice(0, 200)"
                  :key="t.id"
                  :track="t"
                  :index="i + 1"
                  show-album
                  @artist="cat.openArtist"
                  @album="cat.openAlbum"
                  @delete="cat.deleteTrack"
                />
              </div>
              <EmptyState
                v-else
                :message="
                  cat.librarySection.value === 'requests'
                    ? 'sin pedidos'
                    : 'sin canciones'
                "
                :hint="
                  cat.librarySection.value === 'requests'
                    ? 'en discord: !play'
                    : '!play · !fm'
                "
              />
            </div>
          </section>

          <!-- ARTIST DETAIL -->
          <section v-else-if="cat.view.value === 'artist'" class="space-y-6">
            <template v-if="cat.currentArtist.value">
              <div
                class="flex flex-col sm:flex-row gap-5 items-start border border-black/10 p-5 bg-soft"
              >
                <div
                  class="w-28 h-28 sm:w-36 sm:h-36 bg-canvas border border-black/10 overflow-hidden shrink-0"
                >
                  <img
                    v-if="cat.currentArtist.value.cover_url"
                    :src="cat.currentArtist.value.cover_url"
                    alt=""
                    class="w-full h-full object-cover"
                  />
                  <div
                    v-else
                    class="w-full h-full flex items-center justify-center text-3xl text-ash font-bold"
                  >
                    {{ cat.currentArtist.value.name.slice(0, 1) }}
                  </div>
                </div>
                <div class="min-w-0">
                  <p class="text-[11px] text-ash uppercase tracking-wide m-0">
                    Artista
                  </p>
                  <h2 class="text-2xl font-bold text-ink m-0 mt-1">
                    {{ cat.currentArtist.value.name }}
                  </h2>
                  <p class="text-[12px] text-mute mt-2 m-0">
                    {{ cat.currentArtist.value.albums.length }} álbumes ·
                    {{ cat.currentArtist.value.tracks.length }} temas ·
                    {{ cat.currentArtist.value.play_count }} plays
                  </p>
                </div>
              </div>

              <div v-if="cat.currentArtist.value.albums.length">
                <h3 class="text-sm font-bold text-ink mb-3">Álbumes</h3>
                <div
                  class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3"
                >
                  <CoverCard
                    v-for="a in cat.currentArtist.value.albums"
                    :key="a.key"
                    :title="a.name"
                    :subtitle="`${a.track_count} temas`"
                    :cover-url="a.cover_url"
                    @click="cat.openAlbum(a.artist_key, a.key)"
                  />
                </div>
              </div>

              <div>
                <h3 class="text-sm font-bold text-ink mb-3">Canciones</h3>
                <div class="border border-black/10">
                  <TrackRow
                    v-for="(t, i) in cat.currentArtist.value.tracks"
                    :key="t.id"
                    :track="t"
                    :index="i + 1"
                    show-album
                    @artist="cat.openArtist"
                    @album="cat.openAlbum"
                    @delete="cat.deleteTrack"
                  />
                </div>
              </div>
            </template>
            <EmptyState v-else message="artista no encontrado" hint="" />
          </section>

          <!-- ALBUM DETAIL -->
          <section v-else-if="cat.view.value === 'album'" class="space-y-6">
            <template v-if="cat.currentAlbum.value">
              <div
                class="flex flex-col sm:flex-row gap-5 items-start border border-black/10 p-5 bg-soft"
              >
                <div
                  class="w-28 h-28 sm:w-40 sm:h-40 bg-canvas border border-black/10 overflow-hidden shrink-0"
                >
                  <img
                    v-if="cat.currentAlbum.value.cover_url"
                    :src="cat.currentAlbum.value.cover_url"
                    alt=""
                    class="w-full h-full object-cover"
                  />
                </div>
                <div class="min-w-0">
                  <p class="text-[11px] text-ash uppercase tracking-wide m-0">
                    Álbum
                  </p>
                  <h2 class="text-2xl font-bold text-ink m-0 mt-1">
                    {{ cat.currentAlbum.value.name }}
                  </h2>
                  <button
                    type="button"
                    class="text-[13px] text-mute hover:text-ink hover:underline mt-2"
                    @click="
                      cat.openArtist(cat.currentAlbum.value!.artist_key)
                    "
                  >
                    {{ cat.currentAlbum.value.artist }}
                  </button>
                  <p class="text-[12px] text-ash mt-1 m-0">
                    {{ cat.currentAlbum.value.tracks.length }} temas
                  </p>
                </div>
              </div>
              <div class="border border-black/10">
                <TrackRow
                  v-for="(t, i) in cat.currentAlbum.value.tracks"
                  :key="t.id"
                  :track="t"
                  :index="i + 1"
                  @artist="cat.openArtist"
                  @album="cat.openAlbum"
                  @delete="cat.deleteTrack"
                />
              </div>
            </template>
            <EmptyState v-else message="álbum no encontrado" hint="" />
          </section>

          <!-- LIKES -->
          <section v-else-if="cat.view.value === 'likes'" class="space-y-5">
            <h2 class="text-xl font-bold text-ink m-0">Me gusta</h2>
            <div v-if="likedTracks.length" class="border border-black/10">
              <TrackRow
                v-for="(t, i) in likedTracks"
                :key="t.id"
                :track="t"
                :index="i + 1"
                show-album
                @artist="cat.openArtist"
                @album="cat.openAlbum"
                @delete="cat.deleteTrack"
              />
            </div>
            <EmptyState
              v-else
              message="sin likes"
              hint="en discord: !like"
            />
          </section>

          <!-- FM -->
          <section v-else-if="cat.view.value === 'fm'">
            <h2 class="text-xl font-bold text-ink m-0 mb-4">Radio FM</h2>
            <FmSessionPanel
              :sessions="cat.filteredFm.value"
              :selected-id="cat.selectedFmSessionId.value"
              empty-message="sin sesiones FM"
              :station-transitions="transitionsForStation"
              @select="cat.selectedFmSessionId.value = $event"
            />
          </section>

          <!-- ADMIN -->
          <section v-else-if="cat.view.value === 'admin'" class="space-y-6">
            <h2 class="text-xl font-bold text-ink m-0">Admin</h2>
            <p class="text-[12px] text-mute m-0">
              Herramientas de disco y metadata. La experiencia musical está en
              Inicio / Biblioteca.
            </p>
            <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
              <div :class="UI.statTile">
                <div class="text-lg font-bold text-ink">
                  {{ cat.summary.value?.tracks ?? '—' }}
                </div>
                <div class="text-[11px] text-mute">temas</div>
              </div>
              <div :class="UI.statTile">
                <div class="text-lg font-bold text-ink">
                  {{ cat.summary.value?.on_disk ?? '—' }}
                </div>
                <div class="text-[11px] text-mute">en disco</div>
              </div>
              <div :class="UI.statTile">
                <div class="text-lg font-bold text-ink">
                  {{ formatBytes(cat.diskUsage.value.total_bytes) }}
                </div>
                <div class="text-[11px] text-mute">uso</div>
              </div>
              <div :class="UI.statTile">
                <div class="text-lg font-bold text-ink">
                  {{ cat.summary.value?.fm ?? '—' }}
                </div>
                <div class="text-[11px] text-mute">capturas fm</div>
              </div>
            </div>
            <p class="text-[11px] text-ash">
              cache: {{ cat.cacheDir.value || '—' }}
            </p>
            <div class="flex flex-wrap gap-2">
              <button
                type="button"
                :class="UI.btnSecondary"
                :disabled="cat.busyEnrich.value"
                @click="cat.doEnrich()"
              >
                {{
                  cat.busyEnrich.value
                    ? 'enriqueciendo…'
                    : cat.enrichSuggest.value
                      ? `enriquecer · ${cat.enrichSuggest.value}`
                      : 'enriquecer'
                }}
              </button>
              <button
                v-if="(cat.dedupePreview.value?.wasted_bytes || 0) > 0"
                type="button"
                :class="UI.btnDanger"
                :disabled="cat.busyDedupe.value"
                @click="cat.doDedupe()"
              >
                {{
                  cat.busyDedupe.value
                    ? 'limpiando…'
                    : `dedupe · ${formatBytes(cat.dedupePreview.value!.wasted_bytes)}`
                }}
              </button>
              <button
                type="button"
                :class="UI.btnGhost"
                @click="cat.reload()"
              >
                recargar catálogo
              </button>
            </div>
          </section>
        </template>
      </main>
    </div>
  </div>
</template>
