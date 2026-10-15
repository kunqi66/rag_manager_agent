<script setup lang="ts">
import { Lock } from 'lucide-vue-next'

import { MODEL_INFO, MODEL_READONLY_NOTE } from '@/constants/system'

/**
 * 「模型配置」面板 —— **只读信息展示**。
 *
 * 全项目 PRD §8 没有任何模型配置端点（fe-04 §10-1，已登记字典 §9 #73）。
 * 这里刻意**不画成表单**：把只读值渲染成输入框会诱导出「改了怎么不生效」的支持工单，
 * 而模型与阈值本来就该走服务端环境变量 + 重启（CLAUDE.md「常量进配置」、
 * PRD §11.2「密钥自检失败拒绝启动是刻意设计」）。
 *
 * 另一个安全考虑：**不展示任何真实密钥与服务地址的实际值**——
 * 前端拿不到、也不需要拿到（PRD §11.2）。
 */
</script>

<template>
  <div class="model-panel">
    <section class="mpc">
      <header class="mpc__head">
        <h2 class="mpc__title">模型配置</h2>
        <span class="mpc__badge">
          <Lock :size="12" />
          只读
        </span>
      </header>

      <div class="mpc__body">
        <dl class="mpc__list">
          <div v-for="item in MODEL_INFO" :key="item.label" class="mpc__row">
            <dt class="mpc__label">{{ item.label }}</dt>
            <dd class="mpc__value">
              {{ item.value }}
              <span class="mpc__note">{{ item.note }}</span>
            </dd>
          </div>
        </dl>

        <p class="mpc__tip">{{ MODEL_READONLY_NOTE }}</p>
      </div>
    </section>
  </div>
</template>

<style scoped>
.model-panel {
  flex: 1 1 auto;
  min-height: 0;
  min-width: 0;
  display: flex;
}

.mpc {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--kb-bg-card);
  border: 1px solid var(--kb-border);
  border-radius: 10px;
  overflow: hidden;
}

.mpc__head {
  flex: none;
  height: 46px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 14px;
  border-bottom: 1px solid var(--kb-border);
}

.mpc__title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.mpc__badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--kb-bg-subtle);
  font-size: 10.5px;
  color: var(--kb-text-muted);
}

.mpc__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 18px 20px;
}

.mpc__list {
  margin: 0;
  display: grid;
  gap: 14px;
  max-width: 620px;
}

.mpc__row {
  display: grid;
  grid-template-columns: 160px 1fr;
  align-items: baseline;
  gap: 12px;
}

.mpc__label {
  font-size: 12.5px;
  color: var(--kb-text-secondary);
}

.mpc__value {
  margin: 0;
  font-size: 12.5px;
  color: var(--kb-text-primary);
}

.mpc__note {
  margin-left: 8px;
  font-size: 11px;
  color: var(--kb-text-muted);
}

.mpc__tip {
  margin: 20px 0 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--kb-warning-soft);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--kb-warning-text);
}
</style>
