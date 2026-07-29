<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { UI } from '../../ui'

const props = withDefaults(
  defineProps<{
    open: boolean
    title: string
    body?: string
    confirmLabel?: string
    cancelLabel?: string
    danger?: boolean
    busy?: boolean
  }>(),
  {
    body: '',
    confirmLabel: 'confirmar',
    cancelLabel: 'cancelar',
    danger: false,
    busy: false,
  },
)

const emit = defineEmits<{
  confirm: []
  cancel: []
}>()

const confirmBtn = ref<HTMLButtonElement | null>(null)

function onKeydown(e: KeyboardEvent) {
  if (!props.open) return
  if (e.key === 'Escape' && !props.busy) {
    e.preventDefault()
    emit('cancel')
  }
}

watch(
  () => props.open,
  async (open) => {
    if (open) {
      await nextTick()
      confirmBtn.value?.focus()
    }
  },
)

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-ink/40"
      role="presentation"
      @click.self="!busy && emit('cancel')"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        class="w-full max-w-md border border-black/10 bg-canvas p-5 shadow-none"
      >
        <h2
          id="confirm-title"
          class="text-base font-bold text-ink m-0 mb-2"
        >
          {{ title }}
        </h2>
        <p
          v-if="body"
          class="text-sm text-body m-0 mb-5 whitespace-pre-line"
        >
          {{ body }}
        </p>
        <slot />
        <div class="flex flex-wrap justify-end gap-2 mt-5">
          <button
            type="button"
            :class="UI.btnSecondary"
            :disabled="busy"
            @click="emit('cancel')"
          >
            {{ cancelLabel }}
          </button>
          <button
            ref="confirmBtn"
            type="button"
            :class="danger ? UI.btnDanger : UI.btnPrimary"
            :disabled="busy"
            @click="emit('confirm')"
          >
            {{ busy ? '…' : confirmLabel }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
