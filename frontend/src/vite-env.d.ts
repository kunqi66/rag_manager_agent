/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端 API 基址。开发期由 vite proxy 转发、生产由 nginx 反代，通常留空。 */
  readonly VITE_API_BASE_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
