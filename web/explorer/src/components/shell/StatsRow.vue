<script setup lang="ts">
import type { StatTile } from '../../types'
import { UI } from '../../ui'

defineProps<{
  tiles: StatTile[]
  loading?: boolean
}>()
</script>

<template>
  <div class="mb-6">
    <div
      v-if="loading && !tiles.length"
      class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3"
    >
      <div
        v-for="n in 5"
        :key="n"
        :class="[UI.statTile, 'animate-pulse']"
      >
        <div class="h-6 w-12 bg-soft rounded mb-2" />
        <div class="h-3 w-16 bg-soft rounded" />
      </div>
    </div>
    <div
      v-else
      class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3"
    >
      <div
        v-for="(tile, i) in tiles"
        :key="i"
        :class="[
          UI.statTile,
          tile.warn ? 'border-warn/40' : '',
        ]"
      >
        <div
          class="text-lg font-bold truncate leading-tight"
          :class="tile.warn ? 'text-warn' : 'text-ink'"
        >
          {{ tile.value }}
        </div>
        <div
          class="text-[11px] uppercase tracking-wide mt-1 truncate"
          :class="tile.warn ? 'text-warn' : 'text-mute'"
        >
          {{ tile.label }}
        </div>
      </div>
    </div>
  </div>
</template>
