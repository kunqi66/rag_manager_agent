<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import type { DocumentUpdatePayload, KbDocumentVo } from '@/types/kb'

import 'element-plus/es/components/message/style/css'

/**
 * 知识单元编辑弹窗（fe-02 §4.3 ③）。
 *
 * 原型**没有画**这个弹窗（spec §9-16），视觉按 fe-02 §4.3 自拟。
 * 本组件不发请求：打开时 emit `open`，页面据此拉详情；提交只 emit `submit`。
 *
 * ⚠️ 详情接口 `GET /api/import/documents/{id}` **后端未定义**（fe-02 §10 #2，字典 §9 #71），
 * 拿不到 `source_markdown`。fe-02 §10 #2 给了两条路，本实现选**后者**：
 * 正文禁用，本期只允许改标题与分类。
 */
const props = defineProps<{
  visible: boolean
  /** 表格行数据（标题/分类的初值来源） */
  document: KbDocumentVo | null
  /** 详情接口返回的 Markdown 正文。null = 未拿到 */
  sourceMarkdown: string | null
  /** 详情加载中 */
  loading: boolean
  /** 详情加载失败（接口缺失或报错）→ 正文不可编辑 */
  loadError: boolean
  /** 提交中 */
  submitting: boolean
}>()

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'open'): void
  (e: 'submit', payload: DocumentUpdatePayload): void
}>()

const title = ref('')
const category = ref('')
const sourceMarkdown = ref('')

const canEditBody = computed(() => !props.loadError && props.sourceMarkdown !== null)

function resetFromRow(): void {
  title.value = props.document?.title ?? ''
  category.value = props.document?.category ?? ''
  sourceMarkdown.value = props.sourceMarkdown ?? ''
}

// 详情到达后灌入正文（标题/分类此时已经被行数据填过，保持用户可能的修改）
watch(
  () => props.sourceMarkdown,
  (value) => {
    if (value !== null) sourceMarkdown.value = value
  },
)

watch(
  () => props.visible,
  (visible) => {
    if (!visible) return
    resetFromRow()
    emit('open')
  },
)

function onSubmit(): void {
  const trimmed = title.value.trim()
  if (!trimmed) {
    ElMessage.warning('标题不能为空')
    return
  }

  const payload: DocumentUpdatePayload = {
    title: trimmed,
    category: category.value.trim(),
  }
  // 正文只在真的可编辑时才提交：提交一个没读到的空串会把正文清空，
  // 那正是「详情接口缺失」最容易造成的破坏（spec §9-2）。
  if (canEditBody.value) payload.source_markdown = sourceMarkdown.value

  emit('submit', payload)
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    title="编辑知识单元"
    width="640px"
    :close-on-click-modal="false"
    @update:model-value="emit('update:visible', $event)"
  >
    <div v-loading="props.loading" class="edit-dialog">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="提交后将对本文档的切片与向量索引执行全量重建"
        description="重建期间该文档的检索结果可能短暂滞后或缺失，属预期行为（PRD §6.5）。"
      />

      <el-alert
        v-if="props.loadError"
        type="warning"
        :closable="false"
        show-icon
        title="正文读取失败，本次只能修改标题与分类"
        description="后端尚未提供「查询单条详情」接口，无法取到正文原文。直接提交空正文会清空内容，因此正文已禁用（见 spec P1-02 §9-2）。"
      />

      <div class="edit-dialog__field">
        <label class="edit-dialog__label" for="doc-title">标题</label>
        <el-input id="doc-title" v-model="title" maxlength="255" show-word-limit clearable />
      </div>

      <div class="edit-dialog__field">
        <label class="edit-dialog__label" for="doc-category">分类</label>
        <el-input id="doc-category" v-model="category" maxlength="64" clearable placeholder="留空则不归入分类" />
      </div>

      <div class="edit-dialog__field">
        <label class="edit-dialog__label" for="doc-body">正文（Markdown）</label>
        <!-- 纯文本编辑区，不是渲染态：本页不需要 v-html，也就不涉及 DOMPurify。
             将来若做正文预览，必须走 src/utils/markdown.ts 的 renderSafe()（红线 2）。 -->
        <el-input
          id="doc-body"
          v-model="sourceMarkdown"
          type="textarea"
          :rows="14"
          :disabled="!canEditBody"
          resize="vertical"
          placeholder="正文内容"
        />
        <p v-if="canEditBody" class="edit-dialog__hint">
          正文改动会触发全量重建，切片数与向量将在重建完成后更新。
        </p>
      </div>
    </div>

    <template #footer>
      <el-button @click="emit('update:visible', false)">取消</el-button>
      <el-button type="primary" :loading="props.submitting" :disabled="props.loading" @click="onSubmit">
        保存
      </el-button>
      <!-- ⚠️ 保存走后端独立校验（红线 7）：手工直接调 PUT /api/import/documents/{id} 必须 403。 -->
    </template>
  </el-dialog>
</template>

<style scoped>
.edit-dialog {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-height: 120px;
}

.edit-dialog__field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.edit-dialog__label {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--kb-text-secondary);
}

.edit-dialog__hint {
  margin: 0;
  font-size: 11.5px;
  color: var(--kb-text-muted);
}
</style>
