<script setup lang="ts">
import { UI } from '../../ui'

withDefaults(
  defineProps<{
    label?: string
  }>(),
  { label: 'cargando' },
)
</script>

<template>
  <div :class="UI.empty" role="status" aria-live="polite">
    <span
      class="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-black/10 border-t-ink align-middle mr-2"
      aria-hidden="true"
    />
    {{ label }}
  </div>
</template>
