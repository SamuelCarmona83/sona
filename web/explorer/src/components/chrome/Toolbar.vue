<script setup lang="ts">
import { computed } from 'vue'
import type { SortKey, TabId, ViewMode } from '../../types'
import { UI } from '../../ui'
import SegmentedControl from './SegmentedControl.vue'

const props = defineProps<{
  filterText: string
  sortKey: SortKey
  viewMode: ViewMode
  activeTab: TabId
}>()

const emit = defineEmits<{
  'update:filterText': [value: string]
  'update:sortKey': [value: SortKey]
  'update:viewMode': [value: ViewMode]
}>()

const showSort = computed(
  () => props.activeTab !== 'fm' && props.viewMode === 'grid',
)
const showView = computed(() => props.activeTab !== 'fm')

const sortOptions = computed(() => {
  const base: { value: SortKey; label: string }[] = [
    { value: 'recent', label: 'más reciente' },
    { value: 'alpha', label: 'alfabético' },
  ]
  if (props.activeTab === 'searches' || props.activeTab === 'library') {
    base.push({ value: 'duration', label: 'duración' })
  }
  if (props.activeTab === 'library') {
    base.push(
      { value: 'plays', label: 'más reproducidas' },
      { value: 'size', label: 'mayor tamaño' },
    )
  }
  return base
})

const viewOptions = [
  { value: 'grid' as const, label: 'tarjetas', glyph: '▦' },
  { value: 'table' as const, label: 'tabla', glyph: '▤' },
]

function onFilterInput(e: Event) {
  emit('update:filterText', (e.target as HTMLInputElement).value)
}

function clearFilter() {
  emit('update:filterText', '')
}

function onSortChange(e: Event) {
  emit('update:sortKey', (e.target as HTMLSelectElement).value as SortKey)
}
</script>

<template>
  <div class="flex flex-wrap items-center gap-3 mb-6">
    <div class="relative flex-1 min-w-[200px]">
      <label class="sr-only" for="explorer-filter">filtrar</label>
      <input
        id="explorer-filter"
        type="search"
        :class="[UI.input, filterText ? 'pr-9' : '']"
        placeholder="filtrar…"
        autocomplete="off"
        :value="filterText"
        @input="onFilterInput"
      />
      <button
        v-if="filterText"
        type="button"
        class="absolute right-2 top-1/2 -translate-y-1/2 text-ash hover:text-ink text-sm px-1"
        aria-label="limpiar filtro"
        @click="clearFilter"
      >
        [x]
      </button>
    </div>

    <select
      v-if="showSort"
      :class="UI.select"
      :value="sortKey"
      aria-label="ordenar"
      @change="onSortChange"
    >
      <option
        v-for="opt in sortOptions"
        :key="opt.value"
        :value="opt.value"
      >
        {{ opt.label }}
      </option>
    </select>

    <SegmentedControl
      v-if="showView"
      :model-value="viewMode"
      :options="viewOptions"
      aria-label="modo de vista"
      @update:model-value="emit('update:viewMode', $event)"
    />
  </div>
</template>
