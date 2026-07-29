<script setup lang="ts">
import { UI } from '../../ui'

defineProps<{
  message: string
  hint?: string
}>()
</script>

<template>
  <div :class="UI.empty">
    <p class="m-0 text-sm text-mute">{{ message }}</p>
    <p v-if="hint" class="m-0 mt-2 text-[11px] text-ash">{{ hint }}</p>
    <slot />
  </div>
</template>
