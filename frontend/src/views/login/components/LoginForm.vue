<script setup lang="ts">
import { reactive, ref } from 'vue'
import { Info, Lock, LogIn, User } from 'lucide-vue-next'
import type { FormInstance, FormRules } from 'element-plus'

import type { LoginRequest } from '@/types/auth'

/**
 * 登录表单。只做校验 + `emit('submit', payload)`（fe-01 §2）——
 * **不调 store、不跳路由、不发请求**，「登录成功后去哪」由 LoginView 编排。
 *
 * 原型里有、本组件**故意不生成**的三处（详见 P1-01-fe-login spec §9）：
 * 1. 图形验证码 —— 后端无 `GET /api/auth/captcha`（字典 §9 #31）。留一个永远校验不过的
 *    输入框比暂时没有更糟，故整块省略，恢复时连同 rules.captcha 一起加回。
 * 2. 「记住我」—— refresh 一律持久化，无「不记住」分支，2026-10-14 拍板不做（§9-3）。
 * 3. 「忘记密码？」—— fe-01 §1.2 明确不做找回密码（§9-2）。
 */
const props = defineProps<{ loading: boolean }>()

const emit = defineEmits<{ submit: [payload: LoginRequest] }>()

const formRef = ref<FormInstance>()

const form = reactive<LoginRequest>({
  username: '',
  password: '',
})

const rules: FormRules<LoginRequest> = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function onSubmit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  emit('submit', { username: form.username.trim(), password: form.password })
}
</script>

<template>
  <section class="card">
    <h2 class="card__title">欢迎回来</h2>
    <p class="card__sub">请使用企业账号登录，权限将按您的组织归属自动生效</p>

    <el-form
      ref="formRef"
      class="form"
      :model="form"
      :rules="rules"
      label-position="top"
      hide-required-asterisk
      @submit.prevent="onSubmit"
    >
      <el-form-item prop="username" label="用户名">
        <el-input
          v-model="form.username"
          placeholder="请输入用户名"
          autocomplete="username"
          :prefix-icon="User"
        />
      </el-form-item>

      <el-form-item prop="password" label="密码">
        <el-input
          v-model="form.password"
          type="password"
          placeholder="请输入密码"
          autocomplete="current-password"
          show-password
          :prefix-icon="Lock"
          @keyup.enter="onSubmit"
        />
      </el-form-item>

      <el-button
        class="form__submit"
        type="primary"
        :loading="props.loading"
        @click="onSubmit"
      >
        <LogIn v-if="!props.loading" :size="16" />
        <span>登 录</span>
      </el-button>

      <p class="form__hint">
        <Info :size="14" />
        <span>账号由管理员统一分配，不开放自助注册。连续 5 次失败将锁定 15 分钟。</span>
      </p>
    </el-form>
  </section>
</template>

<style scoped>
/* LoginCard 宽 404 —— 来自原型 Bn9SW */
.card {
  width: 404px;
}

.card__title {
  margin: 0;
  font-size: 25px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.card__sub {
  margin: 0;
  padding-top: 8px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--kb-text-secondary);
}

/* Form gap 16 / padding-top 28 —— 来自原型 aEyG0 */
.form {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding-top: 28px;
}

/* el-form-item 默认 margin-bottom 18px，会与上面的 gap 叠加，故清零 */
.form :deep(.el-form-item) {
  display: block;
  margin-bottom: 0;
}

.form :deep(.el-form-item__label) {
  padding-bottom: 6px;
  font-size: 12px;
  font-weight: 500;
  line-height: 1.4;
  color: var(--kb-text-secondary);
}

.form :deep(.el-input__wrapper) {
  height: 42px;
  border-radius: 8px;
  background: var(--kb-bg-subtle);
  box-shadow: 0 0 0 1px var(--kb-border) inset;
}

.form :deep(.el-input__wrapper.is-focus) {
  background: var(--kb-bg-card);
  box-shadow: 0 0 0 1px var(--kb-brand) inset;
}

.form :deep(.el-input__prefix) {
  color: var(--kb-text-placeholder);
}

.form__submit {
  width: 100%;
  height: 44px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 500;
}

.form__submit span {
  margin-left: 6px;
}

.form__hint {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--kb-bg-subtle);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--kb-text-muted);
}

.form__hint svg {
  flex: none;
  margin-top: 2px;
}
</style>
