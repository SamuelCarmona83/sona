<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import ConfirmModal from './components/chrome/ConfirmModal.vue'
import EmptyState from './components/chrome/EmptyState.vue'
import LoadingBlock from './components/chrome/LoadingBlock.vue'
import TabNav from './components/chrome/TabNav.vue'
import Toolbar from './components/chrome/Toolbar.vue'
import DataTable from './components/DataTable.vue'
import FmSessionPanel from './components/FmSessionPanel.vue'
import LibraryToolbar from './components/library/LibraryToolbar.vue'
import AppFooter from './components/shell/AppFooter.vue'
import AppHeader from './components/shell/AppHeader.vue'
import Banner from './components/shell/Banner.vue'
import StatsRow from './components/shell/StatsRow.vue'
import TrackCard from './components/TrackCard.vue'
import { useExplorer } from './composables/useExplorer'
import {
  applyUrlStateOnce,
  watchUrlState,
} from './composables/useUrlState'
import type { SortKey, TabId, ViewMode } from './types'
import { GRID_INITIAL_LIMIT, UI } from './ui'

const ex = useExplorer()

applyUrlStateOnce({
  activeTab: ex.activeTab,
  viewMode: ex.viewMode,
  filterText: ex.filterText,
  setTab: ex.setTab,
  setViewMode: ex.setViewMode,
  onFilterInput: ex.onFilterInput,
})
watchUrlState({
  activeTab: ex.activeTab,
  viewMode: ex.viewMode,
  filterText: ex.filterText,
  setTab: ex.setTab,
  setViewMode: ex.setViewMode,
  onFilterInput: ex.onFilterInput,
})

const hasFilter = computed(() => !!ex.filterText.value.trim())

const tabItems = computed(() => [
  {
    id: 'searches' as TabId,
    label: 'búsquedas',
    count: hasFilter.value
      ? ex.filteredSearches.value.length
      : ex.searches.value.length,
  },
  {
    id: 'library' as TabId,
    label: 'biblioteca',
    count: hasFilter.value || ex.showOutliersOnly.value
      ? ex.filteredLibrary.value.length
      : ex.library.value.length,
  },
  {
    id: 'likes' as TabId,
    label: 'likes',
    count: !ex.secondaryDataLoaded.value
      ? null
      : hasFilter.value
        ? ex.filteredLikes.value.length
        : ex.likes.value.length,
  },
  {
    id: 'fm' as TabId,
    label: 'sesiones FM',
    count: !ex.secondaryDataLoaded.value
      ? null
      : hasFilter.value
        ? ex.filteredFm.value.length
        : ex.fmSessions.value.length,
  },
])

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
    ex.gridLimit.value[tab as 'searches' | 'library' | 'likes'] ||
    GRID_INITIAL_LIMIT
  return items.slice(0, limit)
})

const remainingGrid = computed(
  () => currentItems.value.length - visibleGridItems.value.length,
)

const countLabel = computed(() => {
  const tab = ex.activeTab.value
  if (tab === 'fm') return ''
  const total = currentItems.value.length
  if (!total) return ''
  const parts: string[] = []
  if (hasFilter.value) parts.push(`${total} / filtro`)
  else parts.push(String(total))
  if (tab === 'library' && ex.showOutliersOnly.value) parts.push('outliers')
  return parts.join(' · ')
})

const emptyHint = computed(() => {
  if (ex.showOutliersOnly.value && ex.activeTab.value === 'library') {
    return 'sin outliers con el filtro actual'
  }
  if (hasFilter.value) return 'probá otro filtro o limpiá la búsqueda'
  if (ex.activeTab.value === 'fm') return 'en discord: !fm'
  if (ex.activeTab.value === 'likes') return 'en discord: !like'
  if (ex.activeTab.value === 'library') return 'en discord: !play'
  return 'en discord: !play para cachear búsquedas'
})

const showGroupToggle = computed(
  () =>
    ex.activeTab.value === 'library' && ex.viewMode.value === 'table',
)

function onFilterUpdate(value: string) {
  ex.onFilterInput(value)
}

function onSortUpdate(value: SortKey) {
  ex.sortKey.value = value
}

function onViewUpdate(value: ViewMode) {
  ex.setViewMode(value)
}

function showAllGrid() {
  const tab = ex.activeTab.value as 'searches' | 'library' | 'likes'
  ex.gridLimit.value = {
    ...ex.gridLimit.value,
    [tab]: currentItems.value.length,
  }
}

function onGlobalKeydown(e: KeyboardEvent) {
  const target = e.target as HTMLElement | null
  const tag = target?.tagName
  const typing =
    tag === 'INPUT' ||
    tag === 'TEXTAREA' ||
    tag === 'SELECT' ||
    target?.isContentEditable
  if (e.key === '/' && !typing) {
    e.preventDefault()
    document.getElementById('explorer-filter')?.focus()
    return
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return
  const tabKeys: Record<string, TabId> = {
    '1': 'searches',
    '2': 'library',
    '3': 'likes',
    '4': 'fm',
  }
  const tab = tabKeys[e.key]
  if (tab) {
    e.preventDefault()
    ex.setTab(tab)
  }
}

onMounted(() => {
  window.addEventListener('keydown', onGlobalKeydown)
  void ex.init()
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onGlobalKeydown)
})
</script>

<template>
  <div class="min-h-full bg-canvas text-body font-mono">
    <AppHeader
      :cache-dir="ex.cacheDir.value"
      :loading="ex.loading.value"
    />

    <div :class="[UI.content, 'pb-12']">
      <Banner
        v-if="ex.banner.value"
        :message="ex.banner.value.msg"
        :type="ex.banner.value.type"
        @dismiss="ex.clearBanner()"
      />

      <StatsRow
        :tiles="ex.statsTiles.value"
        :loading="ex.loading.value"
      />

      <Toolbar
        :filter-text="ex.filterText.value"
        :sort-key="ex.sortKey.value"
        :view-mode="ex.viewMode.value"
        :active-tab="ex.activeTab.value"
        @update:filter-text="onFilterUpdate"
        @update:sort-key="onSortUpdate"
        @update:view-mode="onViewUpdate"
      />

      <TabNav
        :tabs="tabItems"
        :active="ex.activeTab.value"
        @select="ex.setTab"
      />

      <main>
        <LoadingBlock v-if="ex.loading.value" label="cargando" />

        <LoadingBlock
          v-else-if="ex.tabBusy.value"
          label="cargando datos…"
        />

        <section v-else-if="ex.activeTab.value === 'fm'">
          <FmSessionPanel
            :sessions="ex.filteredFm.value"
            :selected-id="ex.selectedFmSessionId.value"
            :empty-message="ex.emptyMessage('fm', hasFilter)"
            :station-transitions="ex.transitionsForStation"
            @select="ex.selectedFmSessionId.value = $event"
          />
        </section>

        <section v-else>
          <LibraryToolbar
            v-if="ex.activeTab.value === 'library'"
            :dedupe="ex.dedupePreview.value"
            :show-group-toggle="showGroupToggle"
            :library-grouped="ex.libraryGrouped.value"
            :busy-dedupe="ex.busyDedupe.value"
            :busy-enrich="ex.busyEnrich.value"
            :enrich-suggest="ex.enrichSuggest.value"
            :show-outliers-only="ex.showOutliersOnly.value"
            :outlier-count="ex.outlierCount.value"
            @dedupe="ex.doDedupe()"
            @enrich="ex.doEnrich()"
            @update:library-grouped="ex.setLibraryGrouped"
            @update:show-outliers-only="ex.showOutliersOnly.value = $event"
          />

          <p v-if="countLabel" class="text-[11px] text-ash mb-3">
            {{ countLabel }}
          </p>

          <EmptyState
            v-if="!currentItems.length"
            :message="ex.emptyMessage(ex.activeTab.value, hasFilter)"
            :hint="emptyHint"
          />

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
            <div
              v-if="remainingGrid > 0"
              class="col-span-full flex flex-wrap gap-2"
            >
              <button
                type="button"
                class="flex-1 min-w-[140px] py-3 text-sm font-medium text-ink border border-black/10 hover:bg-soft"
                @click="
                  ex.showMore(
                    ex.activeTab.value as 'searches' | 'library' | 'likes',
                  )
                "
              >
                mostrar más ({{ remainingGrid }} restantes)
              </button>
              <button
                v-if="remainingGrid < GRID_INITIAL_LIMIT * 2"
                type="button"
                class="py-3 px-4 text-sm font-medium text-mute border border-black/10 hover:bg-soft hover:text-ink"
                @click="showAllGrid"
              >
                mostrar todo
              </button>
            </div>
          </div>

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

      <AppFooter hint="/ filtro · 1–4 pestañas" />
    </div>

    <ConfirmModal
      v-if="ex.confirmDialog.value"
      :open="!!ex.confirmDialog.value"
      :title="ex.confirmDialog.value.title"
      :body="ex.confirmDialog.value.body"
      :confirm-label="ex.confirmDialog.value.confirmLabel"
      :cancel-label="ex.confirmDialog.value.cancelLabel"
      :danger="ex.confirmDialog.value.danger"
      :busy="ex.confirmDialog.value.busy"
      @confirm="ex.resolveConfirm(true)"
      @cancel="ex.resolveConfirm(false)"
    />
  </div>
</template>
