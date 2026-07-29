<script setup lang="ts">
import { computed, onMounted } from 'vue'
import DataTable from './components/DataTable.vue'
import FmSessionPanel from './components/FmSessionPanel.vue'
import TrackCard from './components/TrackCard.vue'
import { useExplorer } from './composables/useExplorer'
import type { SortKey, TabId } from './types'
import { formatBytes } from './utils/format'
import { UI } from './ui'

const ex = useExplorer()

const tabs: { id: TabId; label: string }[] = [
  { id: 'searches', label: 'búsquedas' },
  { id: 'library', label: 'biblioteca' },
  { id: 'likes', label: 'likes' },
  { id: 'fm', label: 'sesiones FM' },
]

const hasFilter = computed(() => !!ex.filterText.value.trim())

const currentItems = computed(() => {
  if (ex.activeTab.value === 'searches') return ex.filteredSearches.value
  if (ex.activeTab.value === 'library') return ex.filteredLibrary.value
  if (ex.activeTab.value === 'likes') return ex.filteredLikes.value
  return []
})

const visibleGridItems = computed(() => {
  const tab = ex.activeTab.value
  if (tab === 'fm') return []
  const items = currentItems.value
  const limit =
    ex.gridLimit.value[tab as 'searches' | 'library' | 'likes'] || 36
  return items.slice(0, limit)
})

const countLabel = computed(() => {
  const tab = ex.activeTab.value
  if (tab === 'fm') return ''
  const total = currentItems.value.length
  if (!total) return ''
  return hasFilter.value ? `${total} / filtro` : `${total}`
})

const showDedupe = computed(
  () =>
    !!ex.dedupePreview.value &&
    (ex.dedupePreview.value.wasted_bytes || 0) > 0,
)

const showGroupToggle = computed(
  () =>
    ex.activeTab.value === 'library' && ex.viewMode.value === 'table',
)

function onSortChange(e: Event) {
  ex.sortKey.value = (e.target as HTMLSelectElement).value as SortKey
}

onMounted(() => {
  void ex.init()
})
</script>

<template>
  <header class="bg-ink text-canvas py-5 mb-8">
    <div
      class="max-w-content mx-auto px-5 flex items-baseline justify-between gap-4 flex-wrap"
    >
      <h1 class="text-lg font-bold text-canvas tracking-tight m-0">sona</h1>
      <span class="text-[11px] text-ash">
        {{ ex.cacheDir.value ? `/${ex.cacheDir.value}/` : 'cargando…' }}
      </span>
    </div>
  </header>

  <div class="max-w-content mx-auto px-5 pb-12">
    <div
      v-if="ex.banner.value"
      :class="[
        UI.banner,
        ex.banner.value.type === 'error' ? UI.bannerError : UI.bannerInfo,
      ]"
    >
      {{ ex.banner.value.msg }}
    </div>

    <p class="text-xs text-mute border-b border-black/10 pb-4 mb-5">
      <template v-if="ex.loading.value">cargando…</template>
      <template v-else>
        <template v-for="(part, i) in ex.statsParts.value" :key="i">
          <span v-if="i > 0"> · </span>
          <strong
            class="font-medium"
            :class="part.warn ? 'text-warn' : 'text-ink'"
            >{{ part.text.split(' ')[0] }}</strong
          >
          <span :class="part.warn ? 'text-warn' : ''">
            {{ ' ' + part.text.split(' ').slice(1).join(' ') }}
          </span>
        </template>
      </template>
    </p>

    <div class="flex flex-wrap items-center gap-3 mb-6">
      <div class="flex-1 min-w-[200px]">
        <input
          type="search"
          class="h-10 w-full rounded border border-black/10 bg-soft px-3 text-sm text-ink focus:outline-none focus:border-mute"
          placeholder="filtrar…"
          autocomplete="off"
          :value="ex.filterText.value"
          @input="
            ex.onFilterInput(($event.target as HTMLInputElement).value)
          "
        />
      </div>
      <select
        v-show="ex.activeTab.value !== 'fm' && ex.viewMode.value === 'grid'"
        class="h-10 rounded border border-black/10 bg-soft px-3 text-sm text-ink focus:outline-none cursor-pointer"
        :value="ex.sortKey.value"
        @change="onSortChange"
      >
        <option value="recent">más reciente</option>
        <option value="alpha">alfabético</option>
        <option
          v-show="
            ex.activeTab.value === 'searches' ||
            ex.activeTab.value === 'library'
          "
          value="duration"
        >
          duración
        </option>
        <option v-show="ex.activeTab.value === 'library'" value="plays">
          más reproducidas
        </option>
        <option v-show="ex.activeTab.value === 'library'" value="size">
          mayor tamaño
        </option>
      </select>
      <div
        v-show="ex.activeTab.value !== 'fm'"
        class="flex overflow-hidden rounded border border-black/10"
      >
        <button
          type="button"
          :class="ex.viewMode.value === 'grid' ? UI.btnActive : UI.btn"
          @click="ex.setViewMode('grid')"
        >
          tarjetas
        </button>
        <button
          type="button"
          :class="ex.viewMode.value === 'table' ? UI.btnActive : UI.btn"
          @click="ex.setViewMode('table')"
        >
          tabla
        </button>
      </div>
    </div>

    <nav class="flex border-b border-black/10 mb-6 overflow-x-auto">
      <button
        v-for="t in tabs"
        :key="t.id"
        type="button"
        :class="ex.activeTab.value === t.id ? UI.tabActive : UI.tab"
        @click="ex.setTab(t.id)"
      >
        {{ t.label }}
      </button>
    </nav>

    <main>
      <!-- Loading -->
      <div v-if="ex.loading.value" :class="UI.empty">
        <span
          class="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-black/10 border-t-ink align-middle mr-2"
        />
        cargando
      </div>

      <!-- FM -->
      <section v-else-if="ex.activeTab.value === 'fm'">
        <FmSessionPanel
          :sessions="ex.filteredFm.value"
          :selected-id="ex.selectedFmSessionId.value"
          :empty-message="ex.emptyMessage('fm', hasFilter)"
          :station-transitions="ex.transitionsForStation"
          @select="ex.selectedFmSessionId.value = $event"
        />
      </section>

      <!-- Other tabs -->
      <section v-else>
        <!-- Library toolbar -->
        <div
          v-if="ex.activeTab.value === 'library'"
          class="flex flex-wrap items-center gap-3 mb-4"
        >
          <div
            v-if="showDedupe"
            class="flex-1 min-w-[200px] border border-warn/40 px-3.5 py-2.5 text-sm text-warn"
          >
            {{ ex.dedupePreview.value!.duplicate_groups }} duplicados ·
            {{ formatBytes(ex.dedupePreview.value!.wasted_bytes) }}
            recuperables
          </div>
          <div
            v-if="showGroupToggle"
            class="flex overflow-hidden rounded border border-black/10"
          >
            <button
              type="button"
              :class="ex.libraryGrouped.value ? UI.btnActive : UI.btn"
              @click="ex.setLibraryGrouped(true)"
            >
              agrupado
            </button>
            <button
              type="button"
              :class="!ex.libraryGrouped.value ? UI.btnActive : UI.btn"
              @click="ex.setLibraryGrouped(false)"
            >
              detallado
            </button>
          </div>
          <button
            v-if="showDedupe"
            type="button"
            :class="UI.btnDanger"
            :disabled="ex.busyDedupe.value"
            @click="ex.doDedupe()"
          >
            {{ ex.busyDedupe.value ? 'limpiando…' : 'dedupe' }}
          </button>
          <button
            type="button"
            class="px-4 py-2 text-sm font-medium text-ink border border-black/10 bg-soft rounded hover:bg-white disabled:opacity-50"
            :disabled="ex.busyEnrich.value"
            @click="ex.doEnrich()"
          >
            {{ ex.busyEnrich.value ? 'enriqueciendo…' : 'enriquecer' }}
          </button>
        </div>

        <p class="text-[11px] text-ash mb-3">{{ countLabel }}</p>

        <!-- Empty -->
        <div
          v-if="!currentItems.length"
          :class="UI.empty"
        >
          {{ ex.emptyMessage(ex.activeTab.value, hasFilter) }}
        </div>

        <!-- Grid -->
        <div
          v-else-if="ex.viewMode.value === 'grid'"
          class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4"
        >
          <TrackCard
            v-for="(item, idx) in visibleGridItems"
            :key="idx"
            :tab="ex.activeTab.value"
            :item="item"
            @delete="ex.deleteTrack"
          />
          <button
            v-if="currentItems.length > visibleGridItems.length"
            type="button"
            class="col-span-full py-3 text-sm font-medium text-ink border border-black/10 hover:bg-soft"
            @click="
              ex.showMore(
                ex.activeTab.value as 'searches' | 'library' | 'likes',
              )
            "
          >
            mostrar más ({{
              currentItems.length - visibleGridItems.length
            }}
            restantes)
          </button>
        </div>

        <!-- Table -->
        <DataTable
          v-else
          :tab="ex.activeTab.value"
          :items="currentItems"
          :library-grouped="ex.libraryGrouped.value"
          :table-sort="ex.tableSort.value"
          :total-bytes="ex.diskUsage.value.total_bytes"
          @sort="ex.toggleTableSort"
          @delete="ex.deleteTrack"
        />
      </section>
    </main>

    <footer class="mt-8 text-[11px] text-ash">explorer</footer>
  </div>
</template>
