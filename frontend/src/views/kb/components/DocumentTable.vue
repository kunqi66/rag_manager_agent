<script setup lang="ts">
import { File, FileImage, FileText } from 'lucide-vue-next'
import { computed, nextTick, ref, watch } from 'vue'

import { KB_PERMS, PAGE_SIZE_OPTIONS } from '@/constants/kb'
import { formatDateTime } from '@/utils/format'

import PermissionTag from './PermissionTag.vue'
import StatusTag from './StatusTag.vue'

import type { Component } from 'vue'

import type { KbDocumentVo } from '@/types/kb'

/**
 * 知识单元台账表。列宽逐项对齐原型 `ubgxB`：
 * 标题 fill · 分类 110 · 切片数 90 · 状态 100 · 权限范围 190 · 更新时间 130 · 操作 120；
 * 表头 44 高（bg-subtle + 下边线 border），数据行 52 高（下边线 border-soft），
 * 单元格左右 padding 20。
 *
 * 本组件只 emit 意图，不调 store、不发请求 —— 编排（含二次确认、接口调用、刷新）在页面里。
 *
 * ⚠️ 原型的表格没有分页条，但 fe-02 §4.1/§4.2 定义了 `page/pageSize` 与 `page-change`，
 * 故按设计补一条（spec §9-9）。
 */
const props = defineProps<{
  list: KbDocumentVo[]
  loading: boolean
  total: number
  page: number
  pageSize: number
  /**
   * 需要高亮并滚动入视口的文档 id（跨页跳转定位用）。
   * 空串 = 无定位。定位失败时由页面负责提示，本组件只管「找到了就亮」。
   */
  focusId?: string
}>()

const emit = defineEmits<{
  (e: 'edit', row: KbDocumentVo): void
  (e: 'permission', row: KbDocumentVo): void
  (e: 'toggle-status', row: KbDocumentVo): void
  (e: 'delete', row: KbDocumentVo): void
  (e: 'page-change', page: number, pageSize: number): void
}>()

/**
 * 图标库用显式具名导入 + `as const` 映射：图标名写错**编译期**就报错。
 * 若改用 `:is="`i-lucide-${name}`"` 拼字符串，拼错只会在运行时渲染出一个空白方块。
 * 映射范围对齐原型（只画了 file-text 与 file 两种）。
 */
const FILE_ICONS = {
  md: FileText,
  txt: File,
  pdf: FileText,
  docx: FileText,
  pptx: FileText,
  xlsx: FileText,
  png: FileImage,
  jpg: FileImage,
  jpeg: FileImage,
} as const

/** 返回 `Component` 而不是 `typeof FileText`：lucide 各图标虽同构，
 *  但把它们写成同一个具体组件的类型，一旦上游调整类型定义就会在本行报错。 */
function fileIcon(fileType: string): Component {
  const key = fileType?.toLowerCase() as keyof typeof FILE_ICONS
  return FILE_ICONS[key] ?? FileText
}

/**
 * el-table 默认插槽的 `row` 是 `DefaultRow = Record<PropertyKey, any>`：
 * **读属性没事**（所以模板里 `row.title` 看起来正常），但**不能直接传给形参为具体实体的函数**
 * —— `emit('edit', asDoc(row))` 会因为 `emit` 的重载签名不匹配而报错。
 *
 * 用它而不是在模板里散落 `as KbDocumentVo`：`as` 写错类型不报错，
 * 收窄函数改一处即全组件生效，也留了个写清理由的地方。
 */
function asDoc(row: unknown): KbDocumentVo {
  return row as KbDocumentVo
}

/** 启停按钮文案随当前状态变化：enabled → 「停用」，其余 → 「启用」 */
function toggleLabel(row: KbDocumentVo): string {
  return row.status === 'enabled' ? '停用' : '启用'
}

function onCurrentChange(page: number): void {
  emit('page-change', page, props.pageSize)
}

function onSizeChange(pageSize: number): void {
  emit('page-change', 1, pageSize) // 页长变化时回第一页，否则原页码可能越界
}

// ---- 定位高亮 ----

const cardRef = ref<HTMLElement>()

/**
 * 行类名。用 computed 返回**新函数**而不是直接传一个箭头函数：
 * el-table 只在 `rowClassName` 这个 prop 本身变化时才重算行类，
 * 传固定函数的话 `focusId` 变了表格不会重渲染，高亮永远不出现。
 */
const rowClassName = computed(
  () =>
    (scope: { row: KbDocumentVo }): string =>
      props.focusId && scope.row.id === props.focusId ? 'row--focused' : '',
)

/** 目标行在**表格自己的滚动容器**内居中。不传 block 时浏览器可能只滚一点点，看起来像没反应 */
function scrollFocusedIntoView(): void {
  const row = cardRef.value?.querySelector<HTMLElement>('tr.row--focused')
  row?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}

// 两个触发点都要等：① focusId 设上（同页定位，行已存在）；
// ② list 换页（清空筛选后重新拉取，行才刚出现）。等 nextTick 保证 tr 已经带上了类名。
watch(
  () => props.focusId,
  async (value) => {
    if (!value) return
    await nextTick()
    scrollFocusedIntoView()
  },
)

watch(
  () => props.list,
  async () => {
    if (!props.focusId) return
    await nextTick()
    scrollFocusedIntoView()
  },
)
</script>

<template>
  <div ref="cardRef" class="table-card">
    <el-table
      v-loading="props.loading"
      :data="props.list"
      class="table-card__table"
      :border="false"
      row-key="id"
      :row-class-name="rowClassName"
    >
      <template #empty>
        <span class="table-card__empty">暂无知识单元</span>
      </template>

      <el-table-column label="标题" min-width="200">
        <template #default="{ row }">
          <div class="title-cell">
            <component :is="fileIcon(row.file_type)" :size="15" class="title-cell__icon" />
            <span class="title-cell__name" :title="row.title">{{ row.title || row.file_name }}</span>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="分类" width="110">
        <template #default="{ row }">
          <span class="cell-text">{{ row.category || '—' }}</span>
        </template>
      </el-table-column>

      <el-table-column label="切片数" width="90">
        <template #default="{ row }">
          <span class="cell-text">{{ row.chunk_count }}</span>
        </template>
      </el-table-column>

      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <StatusTag :status="row.status" />
        </template>
      </el-table-column>

      <el-table-column label="权限范围" width="190">
        <template #default="{ row }">
          <PermissionTag :scopes="row.permission_scopes" :summary="row.permission_summary" />
        </template>
      </el-table-column>

      <el-table-column label="更新时间" width="130">
        <template #default="{ row }">
          <span class="cell-text cell-text--muted">{{ formatDateTime(row.updated_at) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="120">
        <template #default="{ row }">
          <div class="actions">
            <!-- 每个写操作各自挂 v-permission。
                 ⚠️ 这只是**显隐体验**，不是安全边界：后端对每个写接口独立校验功能权限
                 （CLAUDE.md 红线 7，spec §6）。手工构造无权限请求必须返回 403。 -->
            <button
              v-permission="KB_PERMS.edit"
              type="button"
              class="actions__link"
              @click="emit('edit', asDoc(row))"
            >
              编辑
            </button>

            <button
              v-permission="KB_PERMS.permission"
              type="button"
              class="actions__link"
              @click="emit('permission', asDoc(row))"
            >
              权限
            </button>

            <button
              v-permission="KB_PERMS.status"
              type="button"
              class="actions__link actions__link--muted"
              :title="row.status === 'enabled' ? '停用后不再参与检索' : '启用后参与检索'"
              @click="emit('toggle-status', asDoc(row))"
            >
              {{ toggleLabel(asDoc(row)) }}
            </button>

            <!-- 删除：原型未画该按钮，按 fe-02 §4.2 的 emits 清单补（spec §9-8）。
                 用 el-popconfirm 做二次确认，避免误删（软删除 + 异步清 Milvus，不可逆代价高）。

                 v-permission 挂在**内层 button** 而不是 el-popconfirm 上：
                 指令的实现是 `el.parentNode.removeChild(el)`，而 el-popconfirm 的根是
                 Tooltip（trigger + teleport 的 popper），根节点不一定是单一元素；
                 挂在触发器上则一定命中一个真实元素。删除是本行最后一个操作，
                 被移除后残留的空 reference 占位在行尾，不产生可见的空隙。 -->
            <el-popconfirm
              title="删除后该知识单元不再参与检索，确定删除？"
              confirm-button-text="删除"
              cancel-button-text="取消"
              confirm-button-type="danger"
              width="240"
              @confirm="emit('delete', asDoc(row))"
            >
              <template #reference>
                <button
                  v-permission="KB_PERMS.remove"
                  type="button"
                  class="actions__link actions__link--danger"
                >
                  删除
                </button>
              </template>
            </el-popconfirm>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页条：原型未画（spec §9-9），放在同一张卡片内以保持视觉连续 -->
    <div class="table-card__footer">
      <el-pagination
        :total="props.total"
        :current-page="props.page"
        :page-size="props.pageSize"
        :page-sizes="[...PAGE_SIZE_OPTIONS]"
        layout="total, sizes, prev, pager, next"
        background
        @current-change="onCurrentChange"
        @size-change="onSizeChange"
      />
    </div>
  </div>
</template>

<style scoped>
/* 原型 ubgxB：fill_container / bg $bg-card / r10 / 1px $border / clip */
.table-card {
  background: var(--kb-bg-card);
  border: 1px solid var(--kb-border);
  border-radius: 10px;
  overflow: hidden;
  /* el-table 的默认描边色改到这里，避免表格内部出现第三种灰 */
  --el-table-border-color: var(--kb-border-soft);
  --el-table-header-bg-color: var(--kb-bg-subtle);
  --el-table-text-color: var(--kb-text-primary);
  --el-table-header-text-color: var(--kb-text-secondary);
  --el-table-row-hover-bg-color: var(--kb-brand-softer);
}

.table-card__table {
  width: 100%;
}

/* 表头 44 高 / bg-subtle / 下边线 1px $border（原型 HeaderRow） */
.table-card__table :deep(th.el-table__cell) {
  height: 44px;
  padding: 0;
  background: var(--kb-bg-subtle);
  border-bottom: 1px solid var(--kb-border);
}

.table-card__table :deep(th.el-table__cell .cell) {
  font-size: 12px;
  font-weight: 600;
  color: var(--kb-text-secondary);
  padding: 0 20px;
  line-height: 1.4;
}

/* 数据行 52 高 / 下边线 1px $border-soft（原型 DataRow） */
.table-card__table :deep(td.el-table__cell) {
  height: 52px;
  padding: 0;
  vertical-align: middle;
}

.table-card__table :deep(td.el-table__cell .cell) {
  padding: 0 20px;
  line-height: 1.4;
}

/* el-table 默认给单元格内的 .cell 加了 overflow:hidden 的文字省略，
   标题列要自己控制截断，这里放开宽度限制避免被外层裁掉 tooltip 的触发区域 */
.table-card__table :deep(.el-table__body-wrapper) {
  overflow-x: hidden;
}

/* 跨页跳转定位到的那一行。用 --kb-brand-soft（比 hover 用的 --kb-brand-softer 深一档），
   否则「高亮行」看起来就只是鼠标恰好停在那一行，用户不知道是被定位过来的。

   选择器刻意写长：el-table 自己的 hover 规则是
   `.el-table__body tr:hover > td.el-table__cell`（0,3,2），
   这里必须压过它，否则鼠标一经过高亮就消失。 */
.table-card :deep(.el-table__body tr.row--focused > td.el-table__cell) {
  background-color: var(--kb-brand-soft);
}

.title-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.title-cell__icon {
  flex: none;
  color: var(--kb-brand);
}

.title-cell__name {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--kb-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.cell-text {
  font-size: 12.5px;
  color: var(--kb-text-secondary);
}

.cell-text--muted {
  font-size: 12px;
  color: var(--kb-text-muted);
}

/* 原型 Actions：gap 12，文字按钮 12/400 */
.actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.actions__link {
  padding: 0;
  border: none;
  background: transparent;
  font-family: inherit;
  font-size: 12px;
  color: var(--kb-brand);
  cursor: pointer;
  white-space: nowrap;
}

.actions__link:hover {
  color: var(--kb-brand-hover);
}

.actions__link--muted {
  color: var(--kb-text-muted);
}

.actions__link--muted:hover {
  color: var(--kb-text-secondary);
}

.actions__link--danger {
  color: var(--kb-text-muted);
}

.actions__link--danger:hover {
  color: var(--kb-danger);
}

.table-card__empty {
  font-size: 12.5px;
  color: var(--kb-text-muted);
}

.table-card__footer {
  display: flex;
  justify-content: flex-end;
  padding: 10px 20px;
  border-top: 1px solid var(--kb-border-soft);
}
</style>
