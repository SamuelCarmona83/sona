<script setup lang="ts">
import { computed } from 'vue'
import type { FmSession, TransitionEdge } from '../types'
import { UI } from '../ui'
import {
  formatDateTime,
  formatDurationSec,
} from '../utils/format'
import { buildTransitions } from '../utils/transform'

const props = defineProps<{
  sessions: FmSession[]
  selectedId: string | null
  emptyMessage: string
  stationTransitions: (stationuuid: string) => TransitionEdge[]
}>()

const emit = defineEmits<{
  select: [id: string]
}>()

const selected = computed(() => {
  if (!props.sessions.length) return null
  const found = props.sessions.find((s) => s.id === props.selectedId)
  return found || props.sessions[0]
})

const sessionEdges = computed(() =>
  selected.value
    ? buildTransitions(selected.value.tracks || []).slice(0, 12)
    : [],
)

const stationEdges = computed(() =>
  selected.value
    ? props.stationTransitions(selected.value.stationuuid).slice(0, 12)
    : [],
)

const countLabel = computed(() => {
  if (!props.sessions.length) return ''
  const total = props.sessions.reduce((n, s) => n + (s.track_count || 0), 0)
  return `${props.sessions.length} sesiones · ${total} detecciones`
})
</script>

<template>
  <div>
    <p class="text-[11px] text-ash mb-3">{{ countLabel }}</p>
    <div
      v-if="!sessions.length"
      class="grid grid-cols-1 lg:grid-cols-5 gap-4"
    >
      <div :class="[UI.empty, 'lg:col-span-2']">{{ emptyMessage }}</div>
      <div class="lg:col-span-3 border border-black/10 min-h-[240px] p-4">
        <p class="text-sm text-mute">{{ emptyMessage }}</p>
      </div>
    </div>
    <div v-else class="grid grid-cols-1 lg:grid-cols-5 gap-4">
      <div
        class="lg:col-span-2 border border-black/10 divide-y divide-black/10 max-h-[70vh] overflow-y-auto"
      >
        <button
          v-for="s in sessions"
          :key="s.id"
          type="button"
          class="w-full text-left px-3 py-2.5 hover:bg-soft"
          :class="
            selected?.id === s.id ? 'bg-soft border-l-2 border-ink' : ''
          "
          @click="emit('select', s.id)"
        >
          <div class="text-sm font-medium text-ink truncate">
            {{ s.station_name }}
          </div>
          <div class="text-[11px] text-mute mt-0.5">
            {{ s.countrycode || '—' }} · {{ s.track_count }} tracks ·
            {{ formatDurationSec(s.duration_sec) }} ·
            <span v-if="s.active" class="text-[10px] text-ok">live</span>
            <span v-else class="text-[10px] text-ash">cerrada</span>
          </div>
          <div class="text-[10px] text-ash mt-0.5">
            {{ formatDateTime(s.started_at) }}
          </div>
        </button>
      </div>

      <div
        v-if="selected"
        class="lg:col-span-3 border border-black/10 min-h-[240px] p-4"
      >
        <div class="mb-4">
          <h2 class="text-base font-bold text-ink">
            {{ selected.station_name }}
          </h2>
          <p class="text-[12px] text-mute mt-1">
            {{ selected.countrycode || '—' }}
            <template v-if="selected.tags"> · {{ selected.tags }}</template>
            · {{ selected.track_count }} tracks ·
            {{ formatDurationSec(selected.duration_sec) }} ·
            {{ selected.active ? 'activa' : 'cerrada' }}
          </p>
          <p class="text-[11px] text-ash mt-0.5">
            {{ formatDateTime(selected.started_at)
            }}{{
              selected.ended_at
                ? ' → ' + formatDateTime(selected.ended_at)
                : ''
            }}
          </p>
        </div>

        <div class="mb-5">
          <h3 class="text-sm font-bold text-ink mb-2">timeline</h3>
          <div v-if="selected.tracks?.length">
            <div
              v-for="(t, i) in selected.tracks"
              :key="i"
              class="flex gap-3 py-2 border-t border-black/10 first:border-t-0"
            >
              <img
                v-if="t.cover_url"
                :src="t.cover_url"
                alt=""
                class="w-9 h-9 object-cover bg-soft shrink-0"
                loading="lazy"
              />
              <div v-else class="w-9 h-9 bg-soft shrink-0" />
              <div class="min-w-0 flex-1">
                <div class="text-sm text-ink truncate">
                  {{ t.artist || '?' }} — {{ t.title || '?' }}
                </div>
                <div class="text-[11px] text-mute">
                  {{ formatDateTime(t.detected_at) }} · #{{ i + 1 }}
                  <template v-if="t.shazam_url">
                    ·
                    <a
                      :href="t.shazam_url"
                      target="_blank"
                      rel="noopener"
                      :class="UI.link"
                      >shazam</a
                    >
                  </template>
                </div>
                <div class="text-[10px] text-ash">
                  {{ t.prev_match_key ? '← prev en sesión' : 'inicio' }}
                </div>
              </div>
            </div>
          </div>
          <p v-else class="text-sm text-mute py-4">
            sin detecciones en esta sesión (shazam aún no matcheó).
          </p>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <h3 class="text-sm font-bold text-ink mb-2">
              transiciones en sesión
            </h3>
            <div v-if="sessionEdges.length">
              <div
                v-for="(e, i) in sessionEdges"
                :key="i"
                class="text-[12px] text-body py-1 border-t border-black/10 first:border-t-0"
              >
                <span class="text-ink">{{ e.fromLabel }}</span>
                <span class="text-ash"> → </span>
                <span class="text-ink">{{ e.toLabel }}</span>
                <span class="text-mute"> ×{{ e.count }}</span>
              </div>
            </div>
            <p v-else class="text-[12px] text-mute">
              hace falta al menos 2 temas seguidos
            </p>
          </div>
          <div>
            <h3 class="text-sm font-bold text-ink mb-2">
              transiciones en esta estación (todas las sesiones)
            </h3>
            <div v-if="stationEdges.length">
              <div
                v-for="(e, i) in stationEdges"
                :key="i"
                class="text-[12px] text-body py-1 border-t border-black/10 first:border-t-0"
              >
                <span class="text-ink">{{ e.fromLabel }}</span>
                <span class="text-ash"> → </span>
                <span class="text-ink">{{ e.toLabel }}</span>
                <span class="text-mute"> ×{{ e.count }}</span>
              </div>
            </div>
            <p v-else class="text-[12px] text-mute">
              sin historial agregado aún
            </p>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
