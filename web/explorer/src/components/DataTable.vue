<script setup lang="ts">
import type {
  LibraryItem,
  LikeItem,
  SearchItem,
  TabId,
  TableColumn,
  TableSort,
} from '../types'
import { COL_WIDTHS, TABLE_COLUMNS, UI } from '../ui'
import {
  formatBytes,
  formatDuration,
  formatTimestamp,
  spotifyUrl,
  youtubeUrl,
} from '../utils/format'
import { isLibraryOutlier, OUTLIER_MAX_BYTES } from '../utils/outliers'

const props = defineProps<{
  tab: TabId
  items: (SearchItem | LibraryItem | LikeItem)[]
  libraryGrouped?: boolean
  tableSort: TableSort
  totalBytes?: number
}>()

const emit = defineEmits<{
  sort: [column: string]
  delete: [trackId: string]
}>()

function columns(): TableColumn[] {
  if (props.tab === 'library' && props.libraryGrouped) {
    return [...TABLE_COLUMNS.libraryGrouped]
  }
  if (props.tab === 'searches') return [...TABLE_COLUMNS.searches]
  if (props.tab === 'library') return [...TABLE_COLUMNS.library]
  if (props.tab === 'likes') return [...TABLE_COLUMNS.likes]
  return []
}

function colWidth(col: TableColumn): string {
  if (col.key === 'actions') return COL_WIDTHS.actions
  if (col.wide) return COL_WIDTHS.wide
  if (col.narrow) return COL_WIDTHS.narrow
  if (col.key === 'artist') return COL_WIDTHS.artist
  return COL_WIDTHS.default
}

function thClass(col: TableColumn): string {
  return col.numeric ? UI.thNum : UI.th
}

function tdClass(col: TableColumn): string {
  if (col.key === 'title') return UI.tdTitle
  return col.numeric ? UI.tdNum : UI.td
}

function sortIndicator(column: string): string {
  if (props.tableSort.column !== column) return '↕'
  return props.tableSort.dir === 'asc' ? '↑' : '↓'
}

function asLib(
  item: SearchItem | LibraryItem | LikeItem,
): LibraryItem | null {
  return props.tab === 'library' ? (item as LibraryItem) : null
}

function asSearch(
  item: SearchItem | LibraryItem | LikeItem,
): SearchItem | null {
  return props.tab === 'searches' ? (item as SearchItem) : null
}

function cellRaw(
  item: SearchItem | LibraryItem | LikeItem,
  key: string,
): string {
  if (key === 'duration') {
    return formatDuration((item as SearchItem).duration)
  }
  if (key === 'cached_at') {
    return formatTimestamp((item as SearchItem).cached_at)
  }
  if (key === 'liked_at') {
    return formatTimestamp((item as LikeItem).liked_at)
  }
  if (key === 'file_size_bytes') {
    const lib = asLib(item)
    return lib?.on_disk ? formatBytes(lib.file_size_bytes) : '—'
  }
  if (key === 'copies') {
    const lib = asLib(item)
    if (!lib || (lib.copies || 0) <= 1) return '—'
    const extra =
      (lib.wasted_bytes || 0) > 0 ? ` ${formatBytes(lib.wasted_bytes)}` : ''
    return `${lib.copies}${extra}`
  }
  if (key === 'play_count') {
    const lib = asLib(item)
    if (lib?.source === 'fm') {
      return String(lib.detect_count || lib.play_count || 0)
    }
    return String((item as LibraryItem).play_count ?? '—')
  }
  if (key === 'request_count') {
    return String((item as SearchItem).request_count ?? '—')
  }
  if (key === 'artist') {
    return String((item as LibraryItem | LikeItem).artist ?? '—')
  }
  if (key === 'album') {
    const lib = asLib(item)
    return lib?.album?.trim() ? lib.album : '—'
  }
  if (key === 'uploader') {
    return String((item as SearchItem).uploader ?? '—')
  }
  return String((item as unknown as Record<string, unknown>)[key] ?? '—')
}

const shownBytes = () =>
  props.items.reduce(
    (s, i) => s + ((i as LibraryItem).file_size_bytes || 0),
    0,
  )
</script>

<template>
  <div :class="UI.tableWrap">
    <table class="w-full table-fixed border-collapse min-w-[640px]">
      <colgroup>
        <col v-for="col in columns()" :key="col.key" :style="{ width: colWidth(col) }" />
      </colgroup>
      <thead class="bg-soft sticky top-0 z-10">
        <tr>
          <th
            v-for="col in columns()"
            :key="col.key"
            :class="thClass(col)"
            scope="col"
            @click="emit('sort', col.key)"
          >
            {{ col.label }}
            <span
              v-if="col.key !== 'actions'"
              :class="
                tableSort.column === col.key ? 'text-accent' : 'text-ash'
              "
              >{{ sortIndicator(col.key) }}</span
            >
          </th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="(item, idx) in items"
          :key="idx"
          :class="[
            UI.rowHover,
            asLib(item) && isLibraryOutlier(asLib(item)!) ? 'bg-warn/5' : '',
          ]"
        >
          <td v-for="col in columns()" :key="col.key" :class="tdClass(col)">
            <template v-if="col.key === 'title'">
              <span class="font-medium text-ink">{{ item.title || '—' }}</span>
              <span
                v-if="asLib(item)?.source === 'fm'"
                class="inline-block text-[10px] font-medium text-accent border border-accent/40 px-1 ml-1 align-middle"
                >fm</span
              >
              <span
                v-if="
                  asSearch(item) &&
                  asSearch(item)!.query &&
                  asSearch(item)!.query !== item.title
                "
                class="block text-[11px] text-ash mt-0.5"
                >{{ asSearch(item)!.query }}</span
              >
              <span class="block text-[11px] mt-1">
                <a
                  v-if="
                    (tab === 'searches' || tab === 'library') &&
                    youtubeUrl(item as SearchItem | LibraryItem)
                  "
                  :href="youtubeUrl(item as SearchItem | LibraryItem)!"
                  target="_blank"
                  rel="noopener"
                  :class="UI.link"
                  >yt</a
                >
                <a
                  v-if="
                    (tab === 'library' || tab === 'likes') &&
                    spotifyUrl((item as LibraryItem | LikeItem).spotify_id)
                  "
                  :href="
                    spotifyUrl((item as LibraryItem | LikeItem).spotify_id)!
                  "
                  target="_blank"
                  rel="noopener"
                  :class="[UI.link, tab === 'library' ? 'ml-1' : '']"
                  >sp</a
                >
              </span>
              <span
                v-if="asLib(item) && isLibraryOutlier(asLib(item)!)"
                class="block text-[10px] text-warn mt-0.5"
                >outlier</span
              >
            </template>
            <template v-else-if="col.key === 'actions'">
              <button
                v-if="asLib(item)?.trackId"
                type="button"
                class="text-[11px] font-medium text-danger hover:underline underline-offset-2"
                title="eliminar de la biblioteca"
                @click="emit('delete', asLib(item)!.trackId)"
              >
                [x]
              </button>
            </template>
            <template v-else-if="col.key === 'file_size_bytes'">
              <span
                :class="
                  asLib(item) &&
                  (asLib(item)!.file_size_bytes || 0) > OUTLIER_MAX_BYTES
                    ? 'text-warn font-medium'
                    : ''
                "
                >{{ cellRaw(item, col.key) }}</span
              >
            </template>
            <template v-else-if="col.key === 'copies'">
              <span
                v-if="asLib(item) && (asLib(item)!.copies || 0) > 1"
                class="text-warn"
                >{{ cellRaw(item, col.key) }}</span
              >
              <span v-else>—</span>
            </template>
            <template v-else>{{ cellRaw(item, col.key) }}</template>
          </td>
        </tr>
      </tbody>
      <tfoot v-if="tab === 'library' && items.length > 0">
        <tr class="bg-soft">
          <td
            :colspan="columns().length"
            class="px-2.5 py-2 text-[11px] text-mute text-right"
          >
            {{ formatBytes(shownBytes()) }} /
            {{ formatBytes(totalBytes || 0) }} · {{ items.length }} filas
          </td>
        </tr>
      </tfoot>
    </table>
  </div>
</template>
