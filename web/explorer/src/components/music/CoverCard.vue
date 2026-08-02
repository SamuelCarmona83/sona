<script setup lang="ts">
defineProps<{
  title: string
  subtitle?: string
  coverUrl?: string
  meta?: string
}>()

const emit = defineEmits<{ click: [] }>()
</script>

<template>
  <button
    type="button"
    class="group text-left w-full border border-black/10 bg-canvas hover:border-mute transition-colors focus:outline-none focus:border-ink"
    @click="emit('click')"
  >
    <div
      class="aspect-square bg-soft overflow-hidden flex items-center justify-center"
    >
      <img
        v-if="coverUrl"
        :src="coverUrl"
        :alt="title"
        class="w-full h-full object-cover group-hover:scale-[1.02] transition-transform duration-300"
        loading="lazy"
      />
      <span
        v-else
        class="text-2xl font-bold text-ash select-none"
        aria-hidden="true"
      >
        {{ (title || '?').slice(0, 1).toUpperCase() }}
      </span>
    </div>
    <div class="p-3">
      <div class="text-sm font-medium text-ink line-clamp-2 m-0">
        {{ title }}
      </div>
      <div v-if="subtitle" class="text-[11px] text-mute mt-0.5 truncate">
        {{ subtitle }}
      </div>
      <div v-if="meta" class="text-[10px] text-ash mt-1">{{ meta }}</div>
    </div>
  </button>
</template>
