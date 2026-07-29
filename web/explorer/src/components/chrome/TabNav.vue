<script setup lang="ts">
import type { TabId } from '../../types'
import { UI } from '../../ui'

export interface TabItem {
  id: TabId
  label: string
  count?: number | string | null
}

const props = defineProps<{
  tabs: TabItem[]
  active: TabId
}>()

const emit = defineEmits<{
  select: [id: TabId]
}>()

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft' && e.key !== 'Home' && e.key !== 'End') {
    return
  }
  e.preventDefault()
  const ids = props.tabs.map((t) => t.id)
  const i = ids.indexOf(props.active)
  if (i < 0) return
  let next = i
  if (e.key === 'ArrowRight') next = (i + 1) % ids.length
  if (e.key === 'ArrowLeft') next = (i - 1 + ids.length) % ids.length
  if (e.key === 'Home') next = 0
  if (e.key === 'End') next = ids.length - 1
  const id = ids[next]
  if (id) {
    emit('select', id)
    document.getElementById(`tab-${id}`)?.focus()
  }
}
</script>

<template>
  <nav
    class="flex border-b border-black/10 mb-6 overflow-x-auto"
    role="tablist"
    aria-label="secciones"
    @keydown="onKeydown"
  >
    <button
      v-for="t in tabs"
      :id="`tab-${t.id}`"
      :key="t.id"
      type="button"
      role="tab"
      :aria-selected="active === t.id"
      :tabindex="active === t.id ? 0 : -1"
      :class="active === t.id ? UI.tabActive : UI.tab"
      @click="emit('select', t.id)"
    >
      <span>{{ t.label }}</span>
      <span
        v-if="t.count != null && t.count !== ''"
        class="ml-1.5 text-[11px] font-normal"
        :class="active === t.id ? 'text-mute' : 'text-ash'"
      >
        {{ t.count }}
      </span>
    </button>
  </nav>
</template>
