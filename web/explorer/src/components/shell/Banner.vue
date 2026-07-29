<script setup lang="ts">
import { onBeforeUnmount, watch } from 'vue'
import type { BannerType } from '../../types'
import { BANNER_AUTO_DISMISS_MS, UI } from '../../ui'

const props = defineProps<{
  message: string
  type?: BannerType
  /** Auto-dismiss non-error banners after delay. Default true. */
  autoDismiss?: boolean
}>()

const emit = defineEmits<{
  dismiss: []
}>()

let timer: ReturnType<typeof setTimeout> | null = null

function clearTimer() {
  if (timer != null) {
    clearTimeout(timer)
    timer = null
  }
}

function scheduleDismiss() {
  clearTimer()
  const type = props.type || 'info'
  const auto = props.autoDismiss !== false
  if (!auto || type === 'error') return
  timer = setTimeout(() => emit('dismiss'), BANNER_AUTO_DISMISS_MS)
}

watch(
  () => [props.message, props.type] as const,
  () => scheduleDismiss(),
  { immediate: true },
)

onBeforeUnmount(clearTimer)

function variantClass(type: BannerType = 'info'): string {
  if (type === 'error') return UI.bannerError
  if (type === 'success') return UI.bannerSuccess
  if (type === 'warn') return UI.bannerWarn
  return UI.bannerInfo
}
</script>

<template>
  <div
    role="status"
    :class="[UI.banner, variantClass(type)]"
  >
    <span class="min-w-0 break-words">{{ message }}</span>
    <button
      type="button"
      class="shrink-0 text-mute hover:text-ink text-sm leading-none px-1"
      aria-label="cerrar"
      @click="emit('dismiss')"
    >
      [x]
    </button>
  </div>
</template>
