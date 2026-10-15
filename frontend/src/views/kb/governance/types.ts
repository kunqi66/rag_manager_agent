/**
 * 治理中心的**视图层**类型（不是后端 DTO —— 那些在 `@/types/governance.ts`）。
 *
 * 放在视图目录里是为了让 `types/` 保持「与 `backend/schemas/` 一一对应」的语义，
 * 不被这类纯前端的中间结构污染。
 */

/** 审核面板的编辑草稿。与候选实体分离，避免边打字边污染列表里那一行 */
export interface ReviewDraft {
  question: string
  answer: string
}

/** 提交审核的载荷。`question`/`answer` 一律取「当前编辑值」，后端以请求体为准（fe-06 §3） */
export interface ReviewSubmit {
  id: string
  action: 'publish' | 'reject'
  question: string
  answer: string
}
