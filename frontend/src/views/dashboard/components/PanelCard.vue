<script setup lang="ts">
/**
 * 看板卡片外壳：标题 + 右侧说明 + 内容区。
 *
 * 原型里 5 张卡共用同一套外壳，只有两处差异：
 * - `bordered`（榜单 / 延时卡）：表头 44 高、带底边框，标题 12.5；
 * - 常规（趋势 / 环形卡）：整卡 padding 14 16、gap 14，标题 13、可带副标。
 *
 * 抽出来的理由：不抽就要在 5 个组件里各写一份标题排版，改一处字号要改 5 个地方，
 * 必然改歪其中一两份。
 */
withDefaults(
  defineProps<{
    /** 卡片标题，逐字对齐原型 */
    title: string
    /** 右侧浅色说明（如「按提问次数」） */
    hint?: string
    /** 标题下方副标（趋势卡的「近 7 天 · 今日提问 N 次」） */
    sub?: string
    /** true = 44 高带底边框的表头变体 */
    bordered?: boolean
    /** true = 整卡内边距变体 */
    padded?: boolean
  }>(),
  { hint: undefined, sub: undefined, bordered: false, padded: false },
)
</script>

<template>
  <section class="panel" :class="{ 'panel--bordered': bordered, 'panel--padded': padded }">
    <header class="panel__head">
      <div class="panel__titlecol">
        <h3 class="panel__title">{{ title }}</h3>
        <p v-if="sub" class="panel__sub">{{ sub }}</p>
      </div>
      <span v-if="hint" class="panel__hint">{{ hint }}</span>
      <div v-if="$slots.extra" class="panel__extra">
        <slot name="extra" />
      </div>
    </header>

    <div class="panel__body">
      <slot />
    </div>
  </section>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  background: var(--kb-bg-card);
  border: 1px solid var(--kb-border);
  border-radius: 10px;
  /* 裁掉子元素的溢出（榜单最后一行的底边框、图表的 canvas 圆角） */
  overflow: hidden;
}

.panel--padded {
  padding: 14px 16px;
  gap: 14px;
}

.panel__head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.panel--bordered .panel__head {
  flex: none;
  height: 44px;
  padding: 0 14px;
  border-bottom: 1px solid var(--kb-border);
}

.panel__titlecol {
  display: flex;
  flex-direction: column;
  gap: 3px;
  flex: 1 1 auto;
  min-width: 0;
}

.panel__title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.panel--bordered .panel__title {
  font-size: 12.5px;
}

.panel__sub {
  margin: 0;
  font-size: 11px;
  color: var(--kb-text-muted);
  /* 桶标签可能很长（小时粒度），超出省略而不是把标题挤变形 */
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.panel__hint {
  flex: none;
  font-size: 10.5px;
  color: var(--kb-text-muted);
}

.panel--padded .panel__hint {
  font-size: 11px;
}

.panel__extra {
  flex: none;
}

.panel__body {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
}
</style>
