/**
 * 跨接口的通用类型。与后端 `backend/core/response.py` 的包装结构一一对应。
 */

/** 后端统一响应包装（CLAUDE.md：所有接口返回 {code, message, data}） */
export interface ApiResponse<T> {
  code: number
  message: string
  data: T
}

/** 分页响应（CLAUDE.md：{list, total, page, page_size}）。登录页不涉及，后续页面用。 */
export interface PageResult<T> {
  list: T[]
  total: number
  page: number
  page_size: number
}
