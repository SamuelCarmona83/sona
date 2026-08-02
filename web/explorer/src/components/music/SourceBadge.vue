<script setup lang="ts">
const props = defineProps<{ sources?: string[]; origin?: string }>()

const label = (): string | null => {
  const s = props.sources || []
  if (s.includes('request') || props.origin === 'request') return 'pedido'
  if (s.includes('fm') || props.origin === 'fm') return 'fm'
  if (s.includes('radio') || props.origin === 'radio') return 'radio'
  if (s.includes('like')) return 'like'
  return null
}

const cls = (): string => {
  const l = label()
  if (l === 'pedido') return 'border-ink/30 text-ink'
  if (l === 'fm') return 'border-accent/40 text-accent'
  if (l === 'radio') return 'border-mute/40 text-mute'
  if (l === 'like') return 'border-danger/40 text-danger'
  return 'border-black/10 text-ash'
}
</script>

<template>
  <span
    v-if="label()"
    class="inline-block text-[10px] font-medium uppercase tracking-wide px-1.5 py-0.5 border"
    :class="cls()"
  >
    {{ label() }}
  </span>
</template>
