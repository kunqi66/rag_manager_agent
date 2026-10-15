<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { FileUp, Trash2, Upload } from 'lucide-vue-next'
import { ElMessage } from 'element-plus'
import type { UploadFile, UploadInstance } from 'element-plus'

import {
  KB_PERMS,
  MAX_UPLOAD_FILE_BYTES,
  MAX_UPLOAD_FILE_MB,
  UPLOAD_ACCEPT_EXTENSIONS,
} from '@/constants/kb'
import { formatFileSize } from '@/utils/format'

import TaskProgressItem from './TaskProgressItem.vue'

import type { ImportTaskVo } from '@/types/kb'

import 'element-plus/es/components/message/style/css'

/**
 * 导入抽屉（fe-02 §4.2 `ImportDrawer`）。
 *
 * 原型**没有画**这个抽屉（全画布检索 drawer/dialog 无命中），视觉按 fe-02 §4.3 自拟，
 * 只复用原型已有的图元与令牌（spec §9-16）。
 *
 * 三条硬约束：
 * 1. `el-upload` 必须 `:auto-upload="false"` —— 它自己上传会**逐文件发请求**，
 *    那样拿不到 `batch_id`，「整批进度」就无从轮询（fe-02 §4.2）。传输交给 `api/import.ts`。
 * 2. 组件只 `emit`，不发请求、不碰 store —— 由页面编排（虚线以上都是同一套分层纪律）。
 * 3. 单文件体积在**本地前置校验**（PRD §6.4.1 单文件 ≤ 200 MB），
 *    别等 413 才告诉用户，那时候文件已经传完了。
 */
const props = defineProps<{
  visible: boolean
  /** 页面在提交中。控制「开始导入」按钮的 loading */
  uploading: boolean
  /** 任务列表（store.sortedTasks），按创建时间倒序 */
  tasks: ImportTaskVo[]
  /** 是否存在失败任务，决定是否展示「全部重试」 */
  hasFailedTask: boolean
  /** 单篇模式：由顶栏「新建知识单元」进入；批量模式：由「批量导入」进入 */
  mode: 'single' | 'batch'
  /**
   * 重置信号。父组件在上传**成功后**自增，本组件据此清空待上传列表与分类。
   * 不用「点按钮即清空」的乐观做法：上传失败（如 413）时把用户选的 10 个文件清掉，
   * 代价远大于收益。也不给 emit 加回调——emit 没有返回值。
   */
  resetToken: number
}>()

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'upload', payload: { files: File[]; category: string }): void
  (e: 'retry', taskId: string): void
  (e: 'retry-all'): void
}>()

const uploadRef = ref<UploadInstance>()
const pendingFiles = ref<File[]>([])
const category = ref('')

const isBatch = computed(() => props.mode === 'batch')
const title = computed(() => (isBatch.value ? '批量导入文档' : '新建知识单元'))
const acceptAttr = computed(() => UPLOAD_ACCEPT_EXTENSIONS.map((ext) => `.${ext}`).join(','))

/** 单篇模式只留一个文件：用户再选一个时替换而不是累加 */
function collect(file: UploadFile): void {
  const raw = file.raw
  if (!raw) return

  if (raw.size > MAX_UPLOAD_FILE_BYTES) {
    ElMessage.warning(`「${raw.name}」超过 ${MAX_UPLOAD_FILE_MB} MB，已跳过`)
    return
  }

  const duplicated = pendingFiles.value.some(
    (item) =>
      item.name === raw.name &&
      item.size === raw.size &&
      item.lastModified === raw.lastModified,
  )
  if (duplicated) return

  pendingFiles.value = isBatch.value ? [...pendingFiles.value, raw] : [raw]
}

// el-upload 的 on-change 会传两个参数，本组件只用第一个。
// 少参签名可赋值给多参函数类型，也避开了 `noUnusedParameters`。
function onChange(file: UploadFile): void {
  collect(file)
}

function removeFile(index: number): void {
  pendingFiles.value = pendingFiles.value.filter((_, i) => i !== index)
}

function clearAll(): void {
  pendingFiles.value = []
  uploadRef.value?.clearFiles()
}

function onSubmit(): void {
  if (pendingFiles.value.length === 0) {
    ElMessage.warning('请先选择要导入的文件')
    return
  }
  emit('upload', { files: [...pendingFiles.value], category: category.value.trim() })
}

// 上传成功后由父组件自增 resetToken 触发清理（见 props 注释）
watch(
  () => props.resetToken,
  () => {
    clearAll()
    category.value = ''
  },
)

// 抽屉每次打开都从干净状态开始：残留上次的文件会让用户误以为自己已经选过了
watch(
  () => props.visible,
  (visible) => {
    if (visible) clearAll()
  },
)
</script>

<template>
  <el-drawer
    :model-value="props.visible"
    :title="title"
    size="520px"
    direction="rtl"
    :close-on-click-modal="false"
    @update:model-value="emit('update:visible', $event)"
  >
    <div class="import-drawer">
      <!-- 上传区 -->
      <section class="import-drawer__section">
        <el-upload
          ref="uploadRef"
          class="import-drawer__upload"
          drag
          multiple
          :directory="isBatch"
          :auto-upload="false"
          :show-file-list="false"
          :accept="acceptAttr"
          :on-change="onChange"
        >
          <div class="upload-zone">
            <FileUp :size="24" class="upload-zone__icon" />
            <p class="upload-zone__title">
              {{ isBatch ? '将文件或文件夹拖到此处，或点击选择' : '将文件拖到此处，或点击选择' }}
            </p>
            <p class="upload-zone__hint">
              支持 {{ UPLOAD_ACCEPT_EXTENSIONS.join(' / ') }}，单文件不超过
              {{ MAX_UPLOAD_FILE_MB }} MB
            </p>
          </div>
        </el-upload>

        <!-- 待上传列表（自绘，因为 el-upload 的默认列表在 auto-upload=false 下交互不完整） -->
        <ul v-if="pendingFiles.length" class="pending">
          <li v-for="(file, index) in pendingFiles" :key="`${file.name}-${file.lastModified}`" class="pending__item">
            <span class="pending__name" :title="file.name">{{ file.name }}</span>
            <span class="pending__size">{{ formatFileSize(file.size) }}</span>
            <button type="button" class="pending__remove" title="移除" @click="removeFile(index)">
              <Trash2 :size="13" />
            </button>
          </li>
        </ul>

        <div class="import-drawer__field">
          <label class="import-drawer__label" for="import-category">分类</label>
          <el-input
            id="import-category"
            v-model="category"
            placeholder="选填，留空则不归入分类"
            maxlength="64"
            clearable
          />
          <!-- 批次的分类是**整批一个值**（接口只收单个 category），UI 上必须说清楚，
               否则用户会以为可以逐文件指定（spec §9-18） -->
          <p v-if="isBatch" class="import-drawer__hint">本批全部文件将归入该分类</p>
        </div>

        <div class="import-drawer__submit">
          <el-button
            v-permission="KB_PERMS.upload"
            type="primary"
            :loading="props.uploading"
            @click="onSubmit"
          >
            <Upload :size="15" />
            <span class="import-drawer__submit-text">开始导入</span>
          </el-button>
          <!-- ⚠️ v-permission 只是显隐体验，不是安全边界：后端对上传接口仍独立校验
               功能权限（CLAUDE.md 红线 7，spec §6）。 -->
        </div>
      </section>

      <!-- 任务进度 -->
      <section class="import-drawer__section">
        <header class="import-drawer__section-head">
          <h2 class="import-drawer__section-title">导入进度</h2>
          <el-button
            v-if="props.hasFailedTask"
            v-permission="KB_PERMS.upload"
            link
            size="small"
            @click="emit('retry-all')"
          >
            全部重试
          </el-button>
        </header>

        <p v-if="props.tasks.length === 0" class="import-drawer__empty">
          尚未提交任何导入任务
        </p>

        <div v-else class="import-drawer__tasks">
          <TaskProgressItem
            v-for="task in props.tasks"
            :key="task.id"
            :task="task"
            @retry="emit('retry', $event)"
          />
        </div>
      </section>
    </div>
  </el-drawer>
</template>

<style scoped>
.import-drawer {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.import-drawer__section-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.import-drawer__section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

/* 拖拽区：沿用原型图元的圆角与描边语言（虚线用 --kb-border） */
.import-drawer__upload :deep(.el-upload) {
  width: 100%;
}

.import-drawer__upload :deep(.el-upload-dragger) {
  padding: 22px 16px;
  border: 1px dashed var(--kb-border);
  border-radius: 8px;
  background: var(--kb-bg-subtle);
  transition: border-color 0.15s;
}

.import-drawer__upload :deep(.el-upload-dragger:hover) {
  border-color: var(--kb-brand);
}

.upload-zone {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}

.upload-zone__icon {
  color: var(--kb-brand);
}

.upload-zone__title {
  margin: 0;
  font-size: 12.5px;
  color: var(--kb-text-primary);
}

.upload-zone__hint {
  margin: 0;
  font-size: 11.5px;
  color: var(--kb-text-muted);
}

.pending {
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
  max-height: 168px;
  overflow: auto;
}

.pending__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 0;
  border-bottom: 1px solid var(--kb-border-soft);
  font-size: 12.5px;
}

.pending__item:last-child {
  border-bottom: none;
}

.pending__name {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--kb-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.pending__size {
  flex: none;
  font-size: 11.5px;
  color: var(--kb-text-muted);
}

.pending__remove {
  flex: none;
  display: inline-flex;
  padding: 2px;
  border: none;
  background: transparent;
  color: var(--kb-text-muted);
  cursor: pointer;
}

.pending__remove:hover {
  color: var(--kb-danger);
}

.import-drawer__field {
  margin-top: 14px;
}

.import-drawer__label {
  display: block;
  margin-bottom: 6px;
  font-size: 12px;
  font-weight: 500;
  color: var(--kb-text-secondary);
}

.import-drawer__hint {
  margin: 6px 0 0;
  font-size: 11.5px;
  color: var(--kb-text-muted);
}

.import-drawer__submit {
  margin-top: 16px;
}

.import-drawer__submit-text {
  margin-left: 6px;
}

.import-drawer__empty {
  margin: 0;
  padding: 18px 0;
  font-size: 12.5px;
  color: var(--kb-text-muted);
  text-align: center;
}

.import-drawer__tasks {
  max-height: 320px;
  overflow: auto;
}
</style>
