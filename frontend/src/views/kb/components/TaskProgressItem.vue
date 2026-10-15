<script setup lang="ts">
import { computed } from 'vue'
import { RefreshCw } from 'lucide-vue-next'

import { KB_PERMS } from '@/constants/kb'
import { formatFileSize, taskStatusLabel } from '@/utils/format'

import type { ImportTaskVo } from '@/types/kb'

/**
 * 单个导入任务的进度行（fe-02 §4.2 `TaskProgressItem`）。
 *
 * 只负责展示与 emit `retry`，不自己发请求——重试由页面编排，
 * 否则轮询/store 的时序会跟组件生命周期纠缠在一起。
 */
const props = defineProps<{
  task: ImportTaskVo
}>()

const emit = defineEmits<{
  (e: 'retry', taskId: string): void
}>()

const isFailed = computed(() => props.task.status === 'failed')
const isSuccess = computed(() => props.task.status === 'success')

/** 进度条配色：成功绿 / 失败红 / 进行中品牌蓝 */
const progressStatus = computed<'success' | 'exception' | undefined>(() => {
  if (isFailed.value) return 'exception'
  if (isSuccess.value) return 'success'
  return undefined
})

const metaText = computed(() =>
  isSuccess.value && props.task.total_chunks > 0
    ? `${formatFileSize(props.task.file_size)} · ${props.task.total_chunks} 个切片`
    : formatFileSize(props.task.file_size),
)

/**
 * 卡片标题。
 *
 * `file_name` 后端是 `str | None`（库里 `kb_document.file_name` 可空，
 * 且 be-09「一键转建」的任务先于文档存在），所以必须有兜底 ——
 * 直接插值 `null` 会渲染出**一张没有名字的空卡片**，
 * 用户看到的是「有个任务但我不知道是哪个文件」。
 */
const displayName = computed(() => props.task.file_name || '未命名文件')
</script>

<template>
  <div class="task-item">
    <div class="task-item__head">
      <span class="task-item__name" :title="props.task.file_name ?? undefined">{{ displayName }}</span>
      <span class="task-item__status" :class="{ 'task-item__status--failed': isFailed }">
        {{ taskStatusLabel(props.task.status) }}
      </span>
    </div>

    <div class="task-item__meta">{{ metaText }}</div>

    <el-progress
      class="task-item__progress"
      :percentage="props.task.progress"
      :status="progressStatus"
      :stroke-width="4"
      :show-text="false"
    />

    <!-- 失败原因：后端 error_msg 是**纯文本**，一律插值渲染，绝不 v-html
         （避免后端的错误信息被当作富文本执行，spec §7 附加纪律） -->
    <p v-if="isFailed && props.task.error_msg" class="task-item__error">
      {{ props.task.error_msg }}
    </p>

    <div v-if="isFailed" class="task-item__actions">
      <!-- 重试的是**导入任务**（kb:document:upload 域）。
           与其它写按钮一样，v-permission 只是显隐，后端仍独立校验（红线 7，
           spec §6）——绕过前端直接调 retry 接口必须 403。 -->
      <el-button
        v-permission="KB_PERMS.upload"
        link
        size="small"
        class="task-item__retry"
        @click="emit('retry', props.task.id)"
      >
        <RefreshCw :size="13" />
        <span class="task-item__retry-text">重试</span>
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.task-item {
  padding: 12px 0;
  border-bottom: 1px solid var(--kb-border-soft);
}

.task-item:last-child {
  border-bottom: none;
}

.task-item__head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.task-item__name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--kb-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.task-item__status {
  flex: none;
  font-size: 11.5px;
  color: var(--kb-text-secondary);
}

.task-item__status--failed {
  color: var(--kb-danger);
}

.task-item__meta {
  margin-top: 3px;
  font-size: 11.5px;
  color: var(--kb-text-muted);
}

.task-item__progress {
  margin-top: 7px;
}

.task-item__error {
  margin: 6px 0 0;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--kb-danger);
  word-break: break-all;
}

.task-item__actions {
  margin-top: 4px;
}

.task-item__retry-text {
  margin-left: 4px;
}
</style>
