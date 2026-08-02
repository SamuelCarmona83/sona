<script setup lang="ts">
import { computed, ref } from 'vue'
import type { FmSession, FmTrack, TransitionEdge } from '../types'
import { UI } from '../ui'
import {
  formatClock,
  formatDateTime,
  formatDurationSec,
  formatGapSince,
} from '../utils/format'
import { buildTransitions } from '../utils/transform'
import EmptyState from './chrome/EmptyState.vue'

const props = defineProps<{
  sessions: FmSession[]
  selectedId: string | null
  emptyMessage: string
  stationTransitions: (stationuuid: string) => TransitionEdge[]
}>()

const emit = defineEmits<{
  select: [id: string]
}>()

const timelineLimit = ref(40)
const TRANSITION_CAP = 12

const selected = computed(() => {
  if (!props.sessions.length) return null
  const found = props.sessions.find((s) => s.id === props.selectedId)
  return found || props.sessions[0]
})

const sessionEdgesAll = computed(() =>
  selected.value ? buildTransitions(selected.value.tracks || []) : [],
)

const stationEdgesAll = computed(() =>
  selected.value
    ? props.stationTransitions(selected.value.stationuuid)
    : [],
)

const sessionEdges = computed(() =>
  sessionEdgesAll.value.slice(0, TRANSITION_CAP),
)
const stationEdges = computed(() =>
  stationEdgesAll.value.slice(0, TRANSITION_CAP),
)

/** Station panel only when it adds history beyond this single session. */
const showStationTransitions = computed(() => {
  if (!selected.value?.stationuuid) return false
  const forStation = props.sessions.filter(
    (s) => s.stationuuid === selected.value!.stationuuid,
  )
  if (forStation.length <= 1) return false
  const sessKeys = new Set(
    sessionEdgesAll.value.map((e) => `${e.from}→${e.to}:${e.count}`),
  )
  const stationKeys = stationEdgesAll.value.map(
    (e) => `${e.from}→${e.to}:${e.count}`,
  )
  if (
    stationKeys.length === sessKeys.size &&
    stationKeys.every((k) => sessKeys.has(k))
  ) {
    return false
  }
  return stationEdgesAll.value.length > 0
})

interface TimelineRow {
  track: FmTrack
  seq: number
  gapSec: number | null
  isStart: boolean
}

const timelineRows = computed((): TimelineRow[] => {
  const tracks = selected.value?.tracks || []
  return tracks.map((t, i) => {
    const prev = i > 0 ? tracks[i - 1] : null
    const gapSec =
      prev && t.detected_at && prev.detected_at
        ? Math.max(0, Number(t.detected_at) - Number(prev.detected_at))
        : null
    const seq =
      t.seq != null && Number.isFinite(Number(t.seq))
        ? Number(t.seq) + 1
        : i + 1
    return {
      track: t,
      seq,
      gapSec,
      isStart: i === 0,
    }
  })
})

const visibleRows = computed(() =>
  timelineRows.value.slice(0, timelineLimit.value),
)

const remainingTracks = computed(() =>
  Math.max(0, timelineRows.value.length - timelineLimit.value),
)

const countLabel = computed(() => {
  if (!props.sessions.length) return ''
  const total = props.sessions.reduce((n, s) => n + (s.track_count || 0), 0)
  return `${props.sessions.length} sesiones · ${total} detecciones`
})

function onListKeydown(e: KeyboardEvent, index: number) {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
  e.preventDefault()
  const next =
    e.key === 'ArrowDown'
      ? Math.min(index + 1, props.sessions.length - 1)
      : Math.max(index - 1, 0)
  const s = props.sessions[next]
  if (s) {
    emit('select', s.id)
    const el = document.getElementById(`fm-session-${s.id}`)
    el?.focus()
  }
}
</script>

<template>
  <div>
    <p v-if="countLabel" class="text-[11px] text-ash mb-3">{{ countLabel }}</p>

    <EmptyState
      v-if="!sessions.length"
      :message="emptyMessage"
      hint="en discord: !fm"
    />

    <div v-else class="grid grid-cols-1 lg:grid-cols-5 gap-4">
      <div
        class="lg:col-span-2 border border-black/10 divide-y divide-black/10 max-h-[70vh] overflow-y-auto"
        role="listbox"
        aria-label="sesiones FM"
      >
        <button
          v-for="(s, index) in sessions"
          :id="`fm-session-${s.id}`"
          :key="s.id"
          type="button"
          role="option"
          :aria-selected="selected?.id === s.id"
          class="w-full text-left px-3 py-2.5 hover:bg-soft focus:outline-none focus:bg-soft"
          :class="
            selected?.id === s.id ? 'bg-soft border-l-2 border-ink' : 'border-l-2 border-transparent'
          "
          @click="emit('select', s.id)"
          @keydown="onListKeydown($event, index)"
        >
          <div class="flex items-start justify-between gap-2">
            <div class="text-sm font-medium text-ink truncate min-w-0">
              {{ s.station_name }}
            </div>
            <span
              v-if="s.active"
              class="shrink-0 text-[10px] font-medium text-ok border border-ok/40 px-1.5 py-0.5"
            >
              live
            </span>
            <span
              v-else
              class="shrink-0 text-[10px] text-ash"
            >
              cerrada
            </span>
          </div>
          <div class="text-[11px] text-mute mt-0.5">
            {{ s.countrycode || '—' }} · {{ s.track_count }} tracks ·
            {{ formatDurationSec(s.duration_sec) }}
          </div>
          <div class="text-[10px] text-ash mt-0.5">
            {{ formatDateTime(s.started_at) }}
          </div>
        </button>
      </div>

      <div
        v-if="selected"
        class="lg:col-span-3 border border-black/10 min-h-[240px] p-4 sm:p-6"
      >
        <div class="mb-5 pb-4 border-b border-black/10">
          <h2 class="text-base font-bold text-ink m-0">
            {{ selected.station_name }}
          </h2>
          <p class="text-[12px] text-mute mt-1 m-0">
            {{ selected.countrycode || '—' }}
            <template v-if="selected.tags"> · {{ selected.tags }}</template>
            · {{ selected.track_count }} tracks ·
            {{ formatDurationSec(selected.duration_sec) }}
            ·
            <span :class="selected.active ? 'text-ok' : 'text-ash'">
              {{ selected.active ? 'activa' : 'cerrada' }}
            </span>
          </p>
          <p class="text-[11px] text-ash mt-1 m-0">
            {{ formatDateTime(selected.started_at)
            }}{{
              selected.ended_at
                ? ' → ' + formatDateTime(selected.ended_at)
                : ''
            }}
          </p>
        </div>

        <!-- Timeline -->
        <div class="mb-6">
          <div class="flex items-baseline justify-between gap-2 mb-3">
            <h3 class="text-sm font-bold text-ink m-0">timeline</h3>
            <span class="text-[11px] text-ash">
              orden de detección · gap = tiempo desde el tema anterior
            </span>
          </div>

          <div v-if="visibleRows.length" class="relative">
            <!-- vertical rail -->
            <div
              class="absolute left-[15px] top-3 bottom-3 w-px bg-black/15"
              aria-hidden="true"
            />

            <ol class="m-0 p-0 list-none space-y-0">
              <li
                v-for="row in visibleRows"
                :key="`${row.seq}-${row.track.detected_at}-${row.track.match_key}`"
                class="relative flex gap-3 py-2.5"
              >
                <!-- node + seq -->
                <div
                  class="relative z-10 w-8 shrink-0 flex flex-col items-center pt-1"
                >
                  <span
                    class="flex h-4 w-4 items-center justify-center rounded-full border-2 text-[9px] font-bold leading-none"
                    :class="
                      row.isStart
                        ? 'border-ink bg-ink text-canvas'
                        : 'border-ink/40 bg-canvas text-ink'
                    "
                    :title="`#${row.seq}`"
                  >
                    {{ row.seq }}
                  </span>
                </div>

                <img
                  v-if="row.track.cover_url"
                  :src="row.track.cover_url"
                  alt=""
                  class="w-10 h-10 object-cover bg-soft shrink-0 border border-black/10"
                  loading="lazy"
                />
                <div
                  v-else
                  class="w-10 h-10 bg-soft shrink-0 border border-black/10"
                  aria-hidden="true"
                />

                <div class="min-w-0 flex-1">
                  <div class="text-sm text-ink leading-snug">
                    <span class="font-medium">{{
                      row.track.artist || '?'
                    }}</span>
                    <span class="text-ash"> — </span>
                    <span>{{ row.track.title || '?' }}</span>
                  </div>
                  <div
                    class="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-mute"
                  >
                    <span class="tabular-nums text-ink/80">{{
                      formatClock(row.track.detected_at)
                    }}</span>
                    <span class="text-ash">·</span>
                    <span
                      v-if="row.isStart"
                      class="text-[10px] uppercase tracking-wide text-ash"
                    >
                      inicio
                    </span>
                    <span
                      v-else-if="row.gapSec != null"
                      class="tabular-nums font-medium text-body"
                      :title="`${row.gapSec}s desde la detección anterior`"
                    >
                      {{ formatGapSince(row.gapSec) }}
                    </span>
                    <template v-if="row.track.shazam_url">
                      <span class="text-ash">·</span>
                      <a
                        :href="row.track.shazam_url"
                        target="_blank"
                        rel="noopener"
                        :class="UI.link"
                        >shazam</a
                      >
                    </template>
                  </div>
                </div>
              </li>
            </ol>

            <button
              v-if="remainingTracks > 0"
              type="button"
              class="mt-2 w-full py-2 text-sm text-mute border border-black/10 hover:bg-soft hover:text-ink"
              @click="timelineLimit += 40"
            >
              +{{ remainingTracks }} más en timeline
            </button>
          </div>
          <p v-else class="text-sm text-mute py-4 m-0">
            sin detecciones en esta sesión (shazam aún no matcheó).
          </p>
        </div>

        <!-- Transitions -->
        <div
          class="grid gap-4"
          :class="
            showStationTransitions
              ? 'grid-cols-1 md:grid-cols-2'
              : 'grid-cols-1'
          "
        >
          <div :class="UI.section">
            <h3 class="text-sm font-bold text-ink mb-1 m-0">
              secuencia A → B
            </h3>
            <p class="text-[10px] text-ash mb-2 m-0">
              pares consecutivos en esta sesión (no es un grafo de “prev”
              genérico)
            </p>
            <div v-if="sessionEdges.length">
              <div
                v-for="(e, i) in sessionEdges"
                :key="i"
                class="text-[12px] text-body py-1.5 border-t border-black/10 first:border-t-0 flex flex-wrap items-baseline gap-x-1"
              >
                <span class="text-ink">{{ e.fromLabel }}</span>
                <span class="text-ash shrink-0">→</span>
                <span class="text-ink">{{ e.toLabel }}</span>
                <span class="text-mute font-medium ml-auto">×{{ e.count }}</span>
              </div>
              <p
                v-if="sessionEdgesAll.length > TRANSITION_CAP"
                class="text-[11px] text-ash mt-2 m-0"
              >
                +{{ sessionEdgesAll.length - TRANSITION_CAP }} más
              </p>
            </div>
            <p v-else class="text-[12px] text-mute m-0">
              hace falta al menos 2 temas seguidos
            </p>
          </div>
          <div v-if="showStationTransitions" :class="UI.section">
            <h3 class="text-sm font-bold text-ink mb-1 m-0">
              misma estación · todas las sesiones
            </h3>
            <p class="text-[10px] text-ash mb-2 m-0">
              agregado histórico de esta emisora
            </p>
            <div v-if="stationEdges.length">
              <div
                v-for="(e, i) in stationEdges"
                :key="i"
                class="text-[12px] text-body py-1.5 border-t border-black/10 first:border-t-0 flex flex-wrap items-baseline gap-x-1"
              >
                <span class="text-ink">{{ e.fromLabel }}</span>
                <span class="text-ash shrink-0">→</span>
                <span class="text-ink">{{ e.toLabel }}</span>
                <span class="text-mute font-medium ml-auto">×{{ e.count }}</span>
              </div>
              <p
                v-if="stationEdgesAll.length > TRANSITION_CAP"
                class="text-[11px] text-ash mt-2 m-0"
              >
                +{{ stationEdgesAll.length - TRANSITION_CAP }} más
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
