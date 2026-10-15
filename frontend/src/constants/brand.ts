/**
 * 品牌面板静态文案。
 *
 * 集中一处而不是写死在组件里（fe-01 §10 #4 / P1-01-fe-login spec §9-6）——
 * 文案是最容易反复改的东西，散在模板里改起来容易漏。
 * 内容逐字取自 pencil 原型 gHen6 / c55di，改文案前先确认原型是否同步。
 */
export const BRAND = {
  productName: '企业知识库管理平台',
  productSub: 'Permission-Aware RAG Knowledge Base',
  tagline: '权限感知检索，让每一次回答都合规、可溯源',
  features: [
    { icon: 'shield-check', text: '四维细粒度权限，无权内容在向量检索阶段即被过滤' },
    { icon: 'search', text: '稠密语义 + BM25 关键词混合检索，引用逐条溯源' },
    { icon: 'layers', text: 'FAQ 聚类沉淀，高频问题免调用生成模型直接命中' },
  ],
  stats: [
    { value: '128', label: '已接入知识单元' },
    { value: '26', label: '平台成员' },
    { value: '1.4s', label: '平均响应时长' },
  ],
} as const
