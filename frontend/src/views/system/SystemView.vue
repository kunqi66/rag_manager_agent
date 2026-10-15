<script setup lang="ts">
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import { onMounted, ref } from 'vue'

import PageTopbar from '@/components/PageTopbar.vue'
import { PANELS, PERM } from '@/constants/system'
import type { PanelKey } from '@/constants/system'
import { useOrgStore } from '@/stores/org'

import DepartmentPanel from './components/DepartmentPanel.vue'
import ModelConfigPanel from './components/ModelConfigPanel.vue'
import RolePanel from './components/RolePanel.vue'
import UserPanel from './components/UserPanel.vue'

/**
 * fe-04 组织架构与系统配置 —— 页面壳。
 *
 * 标题逐字取原型 `zUyFC/RUluS/ljOMC` 的 `PageTitle`：「组织架构与系统配置」。
 * 左侧导航由 `AppLayout` 提供，本页不画（各页共用）。
 *
 * ## 三个设计决定
 *
 * 1. **面板自治**：四个面板各自持有对话框与请求，本壳只管「切哪个」。
 *    fe-04 §4.2 把面板声明成受控组件（props 进、emits 出）会把这个 `setup`
 *    变成一个巨大的状态堆——正是设计 §2「拆分理由」里明确要避免的。
 *    详见 spec §3 的偏差表。
 *
 * 2. **懒挂载 + keep-alive**（fe-04 §2）：四个面板各自存着查询态，切回来不该重置；
 *    但一次性全挂载会同时发起四组请求，拖慢首屏。
 *
 * 3. **顶栏只有一个随 Tab 变化的主按钮**：design 没定义顶栏按钮，原型画的是
 *    「审计日志 + 新建用户」的静态帧（只画了用户管理这一种激活态）。
 *    若把「新建用户」写死，切到部门/角色面板就没有创建入口了。见 spec §9-11。
 */

interface PanelApi {
  openCreate: () => void
}

const org = useOrgStore()

/** 默认激活「用户管理」：原型 `zUyFC` 画的唯一静态帧就是该态（`Seg/用户管理` 为品牌底） */
const activePanel = ref<PanelKey>('user')

const departmentPanel = ref<PanelApi | null>(null)
const userPanel = ref<PanelApi | null>(null)
const rolePanel = ref<PanelApi | null>(null)

/**
 * 基础数据（部门树 / 角色 / 权限树）并行拉一次，三个面板共用。
 * 用户列表由 `UserPanel` 自己拉——它依赖部门筛选条件。
 */
async function bootstrap(): Promise<void> {
  try {
    await org.bootstrap()
  } catch (error) {
    // 单面板失败不该白屏：面板自身有空态文案，这里只把原因报出来
    ElMessage.error(error instanceof Error ? error.message : '基础数据加载失败，请刷新重试')
  }
}

onMounted(() => {
  void bootstrap()
})

function showRolePanel(): void {
  activePanel.value = 'role'
}

/** 「角色与权限概览 → 管理权限点 ›」切过来后，用户可能想直接建角色 */
function openCreateForActivePanel(): void {
  if (activePanel.value === 'department') departmentPanel.value?.openCreate()
  else if (activePanel.value === 'user') userPanel.value?.openCreate()
  else if (activePanel.value === 'role') rolePanel.value?.openCreate()
}
</script>

<template>
  <div class="app-page">
    <PageTopbar title="组织架构与系统配置">
      <!--
        v-permission 只是显隐体验，不是安全边界：后端每个写接口独立校验功能权限
        （CLAUDE.md 红线 7）。绕过前端隐藏直接调接口必须得 403。
      -->
      <el-button
        v-if="activePanel === 'department'"
        v-permission="PERM.deptCreate"
        type="primary"
        @click="openCreateForActivePanel"
      >
        新建部门
      </el-button>
      <el-button
        v-else-if="activePanel === 'user'"
        v-permission="PERM.userCreate"
        type="primary"
        @click="openCreateForActivePanel"
      >
        新建用户
      </el-button>
      <el-button
        v-else-if="activePanel === 'role'"
        v-permission="PERM.roleCreate"
        type="primary"
        @click="openCreateForActivePanel"
      >
        新建角色
      </el-button>
      <!-- 「模型配置」无写入口：无接口，只读展示（fe-04 §10-1） -->
    </PageTopbar>

    <div class="app-page__body">
      <!-- 分段子导航（原型 Y55ZC：圆角 9、内边距 3、gap 2；激活段填充品牌色） -->
      <nav class="subtabs" aria-label="系统管理分段导航">
        <button
          v-for="panel in PANELS"
          :key="panel.key"
          class="seg"
          :class="{ 'is-active': panel.key === activePanel }"
          type="button"
          @click="activePanel = panel.key"
        >
          {{ panel.label }}
        </button>
      </nav>

      <div class="subtabs__panels">
        <KeepAlive>
          <DepartmentPanel v-if="activePanel === 'department'" ref="departmentPanel" />
          <UserPanel
            v-else-if="activePanel === 'user'"
            ref="userPanel"
            @manage-roles="showRolePanel"
          />
          <RolePanel v-else-if="activePanel === 'role'" ref="rolePanel" />
          <ModelConfigPanel v-else />
        </KeepAlive>
      </div>
    </div>
  </div>
</template>

<style scoped>
.subtabs {
  flex: none;
  align-self: flex-start;
  display: flex;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--kb-border);
  border-radius: 9px;
  background: var(--kb-bg-card);
}

.seg {
  padding: 6px 14px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  font-family: inherit;
  font-size: 12.5px;
  color: var(--kb-text-secondary);
  cursor: pointer;
}

.seg.is-active {
  background: var(--kb-brand);
  /* 品牌底上的文字走 --kb-text-on-brand，不要写字面量 #fff */
  color: var(--kb-text-on-brand);
  font-weight: 500;
}

.subtabs__panels {
  flex: 1 1 auto;
  min-height: 0;
  min-width: 0;
  display: flex;
}
</style>
