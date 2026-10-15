<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import type { LoginRequest } from '@/types/auth'

import BrandPanel from './components/BrandPanel.vue'
import LoginForm from './components/LoginForm.vue'

/**
 * 登录页编排层：负责「登录成功后去哪」与失败提示。
 * 表单本身只管 emit，见 LoginForm.vue。
 */
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const loading = ref(false)

/**
 * 只接受站内相对路径。
 * 不校验的话 `?redirect=//evil.com` 会把人带到外站 —— 典型的开放重定向。
 */
function safeRedirect(raw: unknown): string {
  if (typeof raw !== 'string') return '/chat'
  if (!raw.startsWith('/') || raw.startsWith('//')) return '/chat'
  return raw
}

async function onSubmit(payload: LoginRequest): Promise<void> {
  if (loading.value) return
  loading.value = true
  try {
    await auth.login(payload)
    await router.replace(safeRedirect(route.query.redirect))
  } catch (error) {
    // 防账号枚举：只展示后端给的 message，前端不区分「用户不存在」与「密码错误」（spec §9-5）
    ElMessage.error(error instanceof Error ? error.message : '登录失败，请稍后重试')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login">
    <BrandPanel />
    <main class="login__panel">
      <LoginForm :loading="loading" @submit="onSubmit" />
    </main>
  </div>
</template>

<style scoped>
.login {
  display: flex;
  height: 100%;
  background: var(--kb-bg-card);
}

.login__panel {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
</style>
