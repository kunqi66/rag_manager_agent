import type { Directive, DirectiveBinding } from 'vue'

import { useAuthStore } from '@/stores/auth'

/**
 * `v-permission="'kb:document:upload'"` 或 `v-permission="['a', 'b']"`（任一命中即保留）。
 * 码名一律从 `constants/kb.ts` 的 `KB_PERMS` / `constants/system.ts` 的 `PERM` 取，
 * **不要在这里手写字面量** —— 本注释早先举例写的是 `kb:document:create`，
 * 一个从未冻结过的码（字典 §5.6.1 冻结的是 `:upload`），照抄就会做出「按钮永远不显示」。
 *
 * ⚠️ 只控制**显隐**，不是安全边界。每个写接口后端仍独立校验功能权限
 * （CLAUDE.md 安全红线 7）。绕过前端隐藏直接调接口必须得 403 —— 这是 A1 的验收点，
 * 别把它当装饰。
 *
 * 登录页首次定义，供 fe-02 / fe-04 / fe-06 复用，不要在别处再实现一份。
 */

/**
 * 元素被隐藏前的 `display` 原值。
 *
 * 用 WeakMap 而不是 `el.dataset`：后者会在 DOM 上留下痕迹属性，
 * 且元素若被复用到别处会带着脏值。WeakMap 随元素回收，不泄漏。
 */
const originalDisplay = new WeakMap<HTMLElement, string>()

function apply(el: HTMLElement, binding: DirectiveBinding<string | string[] | undefined>): void {
  const codes = Array.isArray(binding.value) ? binding.value : binding.value ? [binding.value] : []
  // 没写权限码 = 不做限制（与 `v-permission=""` 的直觉一致），而不是「全部拒绝」
  const allowed = codes.length === 0 || codes.some((code) => useAuthStore().hasPermission(code))

  if (!originalDisplay.has(el)) originalDisplay.set(el, el.style.display)
  el.style.display = allowed ? (originalDisplay.get(el) ?? '') : 'none'
}

// 泛型带上 `undefined`：`v-permission=""` 在运行时确实会传 undefined，
// 声明成 `string | string[]` 只是把类型撒谎，后面照样得强转。
export const permission: Directive<HTMLElement, string | string[] | undefined> = {
  mounted: apply,
  // 权限可能在挂载后才到齐（如刷新页面时 fetchMe 与首屏渲染的时序），
  // 只在 mounted 判一次会让「有权的人看到隐藏按钮」且再也翻不回来。
  updated: apply,
}

/**
 * 历史实现用 `el.parentNode?.removeChild(el)` 把元素从 DOM 摘掉，有两个问题：
 *
 * 1. 挂在根节点不是单一元素的组件上（如 `el-popconfirm`，根是 Tooltip 的 trigger + popper）
 *    时行为不可预期——Vue 会警告「non-element root node」，指令落点不是你以为的那个节点。
 * 2. 更根本的：Vue 的 patch 仍认为该元素在树里，**后续更新或卸载会去操作一个已脱离的节点**。
 *
 * 改判 `display` 后，元素始终留在树中，patch 正常，且权限变化时可逆。
 * 代价是「DOM 里还在、只是不显示」——但本指令本来就不是安全边界（见文件头），
 * 真正的保证在后端独立校验，所以这个代价可以接受。
 */
