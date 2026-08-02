<script setup lang="ts">
import type { LibraryItem, LikeItem, SearchItem, TabId } from '../types'
import { UI } from '../ui'
import {
  formatBytes,
  formatDuration,
  formatTimestamp,
  spotifyUrl,
  youtubeUrl,
} from '../utils/format'
import { isLibraryOutlier } from '../utils/outliers'

const props = defineProps<{
  tab: TabId
  item: SearchItem | LibraryItem | LikeItem
}>()

const emit = defineEmits<{
  delete: [trackId: string]
}>()

function thumbSrc(): string | undefined {
  const i = props.item as LibraryItem & SearchItem
  return i.best_artwork || i.cover_url || i.thumbnail || undefined
}

function isLib(item: SearchItem | LibraryItem | LikeItem): item is LibraryItem {
  return props.tab === 'library' && 'trackId' in item
}

function isSearch(
  item: SearchItem | LibraryItem | LikeItem,
): item is SearchItem {
  return props.tab === 'searches' && 'query' in item
}

function isLike(item: SearchItem | LibraryItem | LikeItem): item is LikeItem {
  return props.tab === 'likes' && 'liked_at' in item
}
</script>

<template>
  <article
    :class="[
      UI.card,
      'focus-within:border-mute',
      isLib(item) && isLibraryOutlier(item) ? 'border-warn/50' : '',
    ]"
  >
    <div :class="UI.cardThumb">
      <img
        v-if="thumbSrc()"
        :src="thumbSrc()"
        :alt="item.title"
        loading="lazy"
        :class="UI.cardThumbImg"
      />
      <span v-else class="text-3xl text-ash" aria-hidden="true">♪</span>
    </div>
    <div :class="UI.cardBody">
      <div :class="UI.cardTitle">
        {{ item.title }}
        <span
          v-if="isLib(item) && item.source === 'fm'"
          class="inline-block text-[10px] font-medium text-accent border border-accent/40 px-1 ml-1 align-middle"
          >fm</span
        >
        <span
          v-if="isLib(item) && isLibraryOutlier(item)"
          class="inline-block text-[10px] font-medium text-warn border border-warn/40 px-1 ml-1 align-middle"
          >outlier</span
        >
      </div>

      <div v-if="isSearch(item)" :class="UI.cardMeta">
        {{ item.uploader || '—' }}
        <template v-if="item.request_count">
          · ×{{ item.request_count }} pedidos
        </template>
        · {{ formatDuration(item.duration) }} ·
        {{ formatTimestamp(item.cached_at) }}
      </div>
      <div v-else-if="isLib(item)" :class="UI.cardMeta">
        {{ item.artist }}
        ·
        <template v-if="item.source === 'fm'">
          {{ item.detect_count || item.play_count }} det.
          <template v-if="item.station_name">
            · {{ item.station_name }}
          </template>
        </template>
        <template v-else>
          {{ item.play_count }} plays ·
          {{ item.on_disk ? formatBytes(item.file_size_bytes) : '—' }}
        </template>
      </div>
      <div v-else-if="isLike(item)" :class="UI.cardMeta">
        {{ item.artist }} · {{ formatTimestamp(item.liked_at) }}
      </div>

      <div
        v-if="isLib(item) && item.album"
        class="text-[10px] text-ash mt-0.5"
      >
        {{ item.album }}
      </div>

      <div :class="UI.cardLinks">
        <template v-if="isSearch(item)">
          <a
            v-if="youtubeUrl(item)"
            :href="youtubeUrl(item)!"
            target="_blank"
            rel="noopener"
            :class="UI.link"
            >yt</a
          >
        </template>
        <template v-else-if="isLib(item)">
          <a
            v-if="youtubeUrl(item)"
            :href="youtubeUrl(item)!"
            target="_blank"
            rel="noopener"
            :class="UI.link"
            >yt</a
          >
          <template v-if="spotifyUrl(item.spotify_id)">
            <span v-if="youtubeUrl(item)"> · </span>
            <a
              :href="spotifyUrl(item.spotify_id)!"
              target="_blank"
              rel="noopener"
              :class="UI.link"
              >sp</a
            >
          </template>
          <template v-if="item.genius_url">
            ·
            <a
              :href="item.genius_url"
              target="_blank"
              rel="noopener"
              :class="UI.link"
              >genius</a
            >
          </template>
          ·
          <button
            type="button"
            class="text-[11px] font-medium text-danger hover:underline underline-offset-2"
            title="eliminar de la biblioteca"
            @click="emit('delete', item.trackId)"
          >
            [x]
          </button>
        </template>
        <template v-else-if="isLike(item)">
          <a
            v-if="spotifyUrl(item.spotify_id)"
            :href="spotifyUrl(item.spotify_id)!"
            target="_blank"
            rel="noopener"
            :class="UI.link"
            >sp</a
          >
        </template>
      </div>
    </div>
  </article>
</template>
