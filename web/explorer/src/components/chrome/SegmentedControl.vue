<script setup lang="ts" generic="T extends string">
import { UI } from '../../ui'

export interface SegmentOption<T extends string = string> {
  value: T
  label: string
  /** Optional leading glyph, e.g. ▦ */
  glyph?: string
}

const props = defineProps<{
  modelValue: T
  options: SegmentOption<T>[]
  ariaLabel?: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: T]
}>()

function select(value: T) {
  if (value !== props.modelValue) emit('update:modelValue', value)
}
</script>

<template>
  <div
    class="flex overflow-hidden rounded border border-black/10"
    role="group"
    :aria-label="ariaLabel"
  >
    <button
      v-for="opt in options"
      :key="opt.value"
      type="button"
      :class="modelValue === opt.value ? UI.btnActive : UI.btn"
      :aria-pressed="modelValue === opt.value"
      @click="select(opt.value)"
    >
      <span v-if="opt.glyph" class="mr-1" aria-hidden="true">{{ opt.glyph }}</span>
      {{ opt.label }}
    </button>
  </div>
</template>
