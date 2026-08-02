<script setup lang="ts">
import type { CatalogTrack } from '../../types'
import { formatDuration, formatBytes } from '../../utils/format'
import SourceBadge from './SourceBadge.vue'

defineProps<{
  track: CatalogTrack
  index?: number
  showAlbum?: boolean
}>()

const emit = defineEmits<{
  artist: [key: string]
  album: [artistKey: string, albumKey: string]
  delete: [id: string]
}>()
</script>

<template>
  <div
    class="flex items-center gap-3 px-2 sm:px-3 py-2 border-t border-black/10 hover:bg-soft group"
  >
    <span
      v-if="index != null"
      class="w-6 text-[11px] text-ash text-right tabular-nums shrink-0"
      >{{ index }}</span
    >
    <div
      class="w-10 h-10 bg-soft shrink-0 border border-black/10 overflow-hidden"
    >
      <img
        v-if="track.cover_url || track.thumbnail"
        :src="track.cover_url || track.thumbnail"
        alt=""
        class="w-full h-full object-cover"
        loading="lazy"
      />
    </div>
    <div class="min-w-0 flex-1">
      <div class="text-sm text-ink truncate font-medium">
        {{ track.title }}
      </div>
      <div class="text-[11px] text-mute truncate">
        <button
          type="button"
          class="hover:text-ink hover:underline underline-offset-2"
          @click="emit('artist', track.artist_key)"
        >
          {{ track.artist }}
        </button>
        <template v-if="showAlbum && track.album_display">
          <span class="text-ash"> · </span>
          <button
            type="button"
            class="hover:text-ink hover:underline underline-offset-2"
            @click="emit('album', track.artist_key, track.album_key)"
          >
            {{ track.album_display }}
          </button>
        </template>
      </div>
    </div>
    <div class="hidden sm:flex items-center gap-2 shrink-0">
      <SourceBadge :sources="track.sources" :origin="track.origin" />
    </div>
    <div
      class="text-[11px] text-ash tabular-nums w-12 text-right shrink-0 hidden md:block"
    >
      {{
        track.duration
          ? formatDuration(track.duration)
          : track.source === 'fm'
            ? 'live'
            : '—'
      }}
    </div>
    <div
      class="text-[11px] text-ash tabular-nums w-14 text-right shrink-0 hidden lg:block"
    >
      {{
        track.on_disk
          ? formatBytes(track.file_size_bytes)
          : track.source === 'fm'
            ? `${track.detect_count || 1}×`
            : '—'
      }}
    </div>
    <button
      v-if="track.on_disk && !String(track.id).startsWith('fm_')"
      type="button"
      class="text-[11px] text-ash hover:text-danger opacity-0 group-hover:opacity-100 shrink-0 px-1"
      title="eliminar"
      @click="emit('delete', track.id)"
    >
      ✕
    </button>
  </div>
</template>
