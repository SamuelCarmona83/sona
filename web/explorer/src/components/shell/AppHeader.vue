<script setup lang="ts">
defineProps<{
  cacheDir: string | null
  loading?: boolean
}>()
</script>

<template>
  <header class="bg-ink text-on-dark mb-8">
    <div :class="['max-w-content mx-auto px-5 py-8 sm:py-10']">
      <div class="flex flex-col gap-6 sm:flex-row sm:items-end sm:justify-between">
        <div class="min-w-0">
          <p class="text-[11px] text-on-dark-mute mb-2 tracking-wide">
            [+] data explorer
          </p>
          <h1
            class="text-[22px] sm:text-[28px] font-bold text-on-dark tracking-tight m-0 leading-tight"
          >
            sona
          </h1>
          <p class="mt-2 text-[12px] text-on-dark-mute truncate">
            <template v-if="loading && !cacheDir">cargando caché…</template>
            <template v-else-if="cacheDir">cache: /{{ cacheDir }}/</template>
            <template v-else>cache: —</template>
          </p>
        </div>

        <div
          class="shrink-0 rounded bg-charcoal px-4 py-3 text-[12px] text-on-dark-mute font-mono max-w-full overflow-x-auto"
          aria-hidden="true"
        >
          <span class="text-on-dark">></span> !play · !fm · !like
        </div>
      </div>
    </div>
  </header>
</template>
