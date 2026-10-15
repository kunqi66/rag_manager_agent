<script setup lang="ts">
/**
 * 用户提问气泡。原型 `NtZEu/UserMsg/UserBubble`（`gK3ee`）。
 *
 * 纯展示：用户内容**一律插值**，绝不进 `v-html`——
 * 提问是用户输入，把它当富文本渲染等于自造一个 XSS 入口。
 */
defineProps<{
  content: string
}>()
</script>

<template>
  <div class="user-msg">
    <p class="user-msg__bubble">{{ content }}</p>
  </div>
</template>

<style scoped>
/* UserMsg：整行右对齐（原型 justifyContent: end） */
.user-msg {
  display: flex;
  justify-content: flex-end;
}

/* UserBubble：bg $brand，圆角 [12,12,3,12]，padding [11,15] */
.user-msg__bubble {
  max-width: 640px;
  margin: 0;
  padding: 11px 15px;
  border-radius: 12px 12px 3px 12px;
  background: var(--kb-brand);
  color: var(--kb-text-on-brand);
  font-size: 13.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
