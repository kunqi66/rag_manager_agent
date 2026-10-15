<script setup lang="ts">
import { ShieldAlert } from 'lucide-vue-next'
import { computed } from 'vue'

import { BLOCKED_DEMO_NOTICE, BLOCKED_TEXT_TEMPLATE } from '@/constants/chat'

/**
 * 权限缺失气泡。原型 `WTK0N/AiBody/BlockedBubble`（`bZ4k9`）。
 *
 * 🔴 **红线 4（PRD R9）**：
 * 1. `titles` 是**可选**的，缺省即只渲染数量文案——**不要**写 `titles.map(...)` 而不判空；
 * 2. 即使有 `titles`，也只渲染**标题**，绝不渲染正文片段；
 * 3. 本组件的输入是后端算好的**精确数量**，前端**不做任何权限判断**，也不
 *    「试着探测一下有没有敏感文档」；
 * 4. 载荷原始 JSON **不进 console、不进任何埋点**（防信息侧信道外泄）。
 *
 * 气泡是**信息型**的，不是错误——受限是预期行为，不是故障。
 */
const props = defineProps<{
  count: number
  /** 仅当后端开了 ACL_REVEAL_BLOCKED_TITLES（默认 false）的演示开关时才有值 */
  titles?: string[]
}>()

const text = computed(() => BLOCKED_TEXT_TEMPLATE.replace('{count}', String(props.count)))

// 显式判空：默认口径下 titles 不存在，此时**只有数量**
const hasTitles = computed(() => Array.isArray(props.titles) && props.titles.length > 0)
</script>

<template>
  <div class="blocked">
    <div class="blocked__row">
      <ShieldAlert :size="17" class="blocked__icon" />
      <span class="blocked__text">{{ text }}</span>
    </div>

    <!-- 演示模式才走这条分支：仍只给标题，不给正文 -->
    <template v-if="hasTitles">
      <p class="blocked__notice">{{ BLOCKED_DEMO_NOTICE }}</p>
      <ul class="blocked__titles">
        <li v-for="title in titles" :key="title">{{ title }}</li>
      </ul>
    </template>
  </div>
</template>

<style scoped>
/* BlockedBubble：bg $warning-soft，圆角 9，描边 1px（原型画布里的硬编码色值已收编为
   --kb-warning-border，本文件不出现十六进制） */
.blocked {
  padding: 11px 14px;
  border-radius: 9px;
  background: var(--kb-warning-soft);
  border: 1px solid var(--kb-warning-border);
}

.blocked__row {
  display: flex;
  align-items: center;
  gap: 11px;
}

.blocked__icon {
  flex: none;
  color: var(--kb-warning);
}

/* BlockedText：12.5，颜色用 --kb-warning-text（原型硬编码值已收编） */
.blocked__text {
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--kb-warning-text);
}

.blocked__notice {
  margin: 8px 0 0;
  font-size: 11px;
  color: var(--kb-warning);
}

.blocked__titles {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--kb-warning-text);
}
</style>
