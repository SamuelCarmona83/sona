<script setup lang="ts">
import type { DedupePreview, LibraryGroupMode } from '../../types'
import { formatBytes } from '../../utils/format'
import { UI } from '../../ui'
import SegmentedControl from '../chrome/SegmentedControl.vue'

defineProps<{
  dedupe: DedupePreview | null
  showGroupToggle: boolean
  libraryGroupMode: LibraryGroupMode
  busyDedupe: boolean
  busyEnrich: boolean
  enrichSuggest?: number | null
  showOutliersOnly?: boolean
  outlierCount?: number
}>()

const emit = defineEmits<{
  dedupe: []
  enrich: []
  'update:libraryGroupMode': [value: LibraryGroupMode]
  'update:showOutliersOnly': [value: boolean]
}>()

const groupOptions: { value: LibraryGroupMode; label: string }[] = [
  { value: 'flat', label: 'lista' },
  { value: 'artist', label: 'artista' },
  { value: 'album', label: 'álbum' },
  { value: 'video', label: 'video' },
]
</script>

<template>
  <div class="flex flex-wrap items-center gap-3 mb-4">
    <div
      v-if="dedupe && (dedupe.wasted_bytes || 0) > 0"
      class="flex-1 min-w-[200px] border border-warn/40 px-3.5 py-2.5 text-sm text-warn"
    >
      <span class="font-medium">
        {{ dedupe.duplicate_groups }} duplicados
      </span>
      · {{ formatBytes(dedupe.wasted_bytes) }} recuperables
      <span
        v-if="dedupe.files_to_delete?.length"
        class="text-[11px] text-mute ml-1"
      >
        · {{ dedupe.files_to_delete.length }} archivos
      </span>
    </div>

    <SegmentedControl
      v-if="showGroupToggle"
      :model-value="libraryGroupMode"
      :options="groupOptions"
      aria-label="agrupación de biblioteca"
      @update:model-value="emit('update:libraryGroupMode', $event)"
    />

    <button
      v-if="outlierCount != null && outlierCount > 0"
      type="button"
      :class="showOutliersOnly ? UI.btnActive : UI.btn"
      class="!border !border-black/10 rounded"
      @click="emit('update:showOutliersOnly', !showOutliersOnly)"
    >
      outliers {{ outlierCount }}
    </button>

    <button
      v-if="dedupe && (dedupe.wasted_bytes || 0) > 0"
      type="button"
      :class="UI.btnDanger"
      :disabled="busyDedupe"
      @click="emit('dedupe')"
    >
      {{ busyDedupe ? 'limpiando…' : 'dedupe' }}
    </button>

    <button
      type="button"
      :class="UI.btnSecondary"
      :disabled="busyEnrich"
      @click="emit('enrich')"
    >
      <template v-if="busyEnrich">enriqueciendo…</template>
      <template v-else-if="enrichSuggest != null && enrichSuggest > 0">
        enriquecer · {{ enrichSuggest }}
      </template>
      <template v-else>enriquecer</template>
    </button>
  </div>
</template>
