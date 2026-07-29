<script setup lang="ts">
defineProps<{
  hint?: string
}>()
</script>

<template>
  <footer class="mt-10 pt-6 border-t border-black/10 text-[11px] text-ash flex flex-wrap gap-x-3 gap-y-1">
    <span>explorer · vue</span>
    <span v-if="hint" class="text-mute">{{ hint }}</span>
  </footer>
</template>
