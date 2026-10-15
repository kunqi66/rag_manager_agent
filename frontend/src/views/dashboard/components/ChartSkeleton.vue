<script setup lang="ts">
/**
 * 图表 / 榜单的骨架屏。
 *
 * **关键是「固定占满父容器高度」**：数据到达时若占位高度与实际图表高度不同，
 * 整个栅格会往下/往上跳一下（CLS 抖动），用户会以为自己点错了东西。
 * 所以骨架的尺寸必须跟最终内容一致，而不是「大概这么高」。
 */
const BAR_HEIGHTS = ['38%', '64%', '46%', '80%', '58%', '72%', '50%'] as const
</script>

<template>
  <div class="chart-skeleton">
    <!-- ⚠️ `:loading` 必须显式传 true：`el-skeleton` 的默认值是 **false**，
         此时它渲染默认插槽（这里是空的）而**不渲染 `#template`** —— 骨架会整个消失。 -->
    <el-skeleton animated :loading="true">
      <template #template>
        <div class="chart-skeleton__bars">
          <el-skeleton-item
            v-for="(height, index) in BAR_HEIGHTS"
            :key="index"
            variant="rect"
            class="chart-skeleton__bar"
            :style="{ height }"
          />
        </div>
      </template>
    </el-skeleton>
  </div>
</template>

<style scoped>
.chart-skeleton {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  padding: 4px 0;
}

.chart-skeleton :deep(.el-skeleton) {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
}

.chart-skeleton__bars {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  align-items: flex-end;
  gap: 10px;
}

.chart-skeleton__bar {
  flex: 1 1 0;
  min-width: 0;
  border-radius: 3px;
}
</style>
