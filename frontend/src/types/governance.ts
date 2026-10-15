/**
 * 治理中心（知识沉淀与运营）相关 DTO。与后端 `backend/schemas/mining.py` 一一对应。
 *
 * ✅ **be-08 已实现（2026-10-14）**，本文件里 be-08 的 9 个 interface 已**逐字段核对**
 * 过 `backend/schemas/mining.py`（`backend/scripts/verify_faq.py` 的 T1 会自动对账，
 * 前端要而后端缺的字段会直接判 FAIL —— 这条对账不靠人眼，因为字段名对不上
 * **不会编译报错**，只会让 `data.xxx` 取到 `undefined`，而接口照回 200）。
 *
 * ✅ **be-09（知识缺口闭环）已实现（2026-10-14）**，`KnowledgeGapItem` / `GapQuery` /
 * `GapTaskResult` 三个 interface 已**逐字段核对**过 `backend/schemas/mining.py`。
 * 两处**撤回/收紧**的字段见下方各自的注释（`department_name` 撤回、
 * `GapTaskResult.task_id` 由可选变必回）。
 *
 * 命名口径：按字典 §8「后端 `UserDetail` → 前端 `interface UserDetail`（同名）」，
 * 取后端名 `...Item`（fe-06 设计里写的 `...Vo` 是旧名，见 spec §9-12）。
 */

/** 候选 FAQ 审核状态（字典 §5.4 ✅） */
export type CandidateStatus = 'pending' | 'published' | 'rejected'

/** 知识缺口状态（字典 §5.4 ✅） */
export type GapStatus = 'open' | 'tasked' | 'closed'

/** 关联知识单元的精简信息。✅ be-08 已在候选列表里内联返回（无权的整条剔除） */
export interface RelatedDocBrief {
  id: string
  title: string
}

/**
 * 候选 FAQ 条目（字典 §2.4 `faq_candidate`）。
 *
 * 注意**不定义** `question_embedding`——它只服务后端的进程内索引与相似度聚合，
 * 对展示层毫无用途，从类型里排除就是从契约上表达「它不属于前端」。
 */
export interface FaqCandidateItem {
  id: string
  /** 聚类簇内部标识。**仅用于列表 key 的稳定，不展示**（fe-06 §5） */
  cluster_key: string
  /** 代表性问题（簇内距质心最近的那条） */
  question: string
  /** 推荐标准答案（DeepSeek 生成 → 属 LLM 输出，见 spec §7 红线 2） */
  answer: string
  frequency: number
  /** 0–1，字典 §2.4 */
  confidence: number
  /** 关联知识单元 ID 数组（字典 §2.4 存的是 ID） */
  related_doc_ids: string[]
  /** ✅ 后端内联的标题；无权的文档**整条不在数组里**（红线 4，不是空标题） */
  related_docs?: RelatedDocBrief[]
  status: CandidateStatus
  reviewed_by: string | null
  reviewed_at: string | null
  created_at: string
}

/** 已发布 FAQ（字典 §2.4 `faq`）。同样不定义 `question_embedding`。 */
export interface FaqItem {
  id: string
  question: string
  answer: string
  /** 是否参与进程内索引的毫秒级匹配（PRD §6.3.2） */
  cache_enabled: boolean
  /** 来源候选 ID，手工新建的 FAQ 不存在——本页也不提供手工新建 */
  source_candidate_id: string | null
  published_by: string
  published_at: string
  /** 缓存机制的效果度量 */
  hit_count: number
}

/** 知识缺口（字典 §2.4 `knowledge_gap`）。不定义 `question_embedding`。 */
export interface KnowledgeGapItem {
  id: string
  question: string
  /** 提问部门 ID。🔒 表里**没有 user_id**，前端不得凭空拼出提问人身份（spec §7 红线 4） */
  department_id: string | null
  // 🔴 **本字段刻意没有 `department_name`**（字典 §9 #146 已撤回）。
  //    缺口可能来自**别的**部门，而前端只有自己那条部门链 —— 后端不内联名字，
  //    前端也查不到，`department_name` 只会永远是 `undefined`。
  //    展示层降级显示 `department_id`；真需要部门名得 be-02 另开一个批量取名接口。
  frequency: number
  /** 最高相似度，可空 */
  max_score: number | null
  /** DeepSeek 推荐的分类，失败时留空（be-09 §4.5） */
  suggested_category: string | null
  status: GapStatus
  last_seen_at: string
  created_at: string
}

/**
 * 审核请求体 —— **与后端 `schemas/mining.py::FaqReviewPayload` 同名同构（✅）**。
 *
 * `question` / `answer` 是**人工修正后的当前值**：用户没改也要原样提交，
 * 后端以请求体为准，**不做「空值就用原推荐答案」的兜底猜测**（fe-06 §3）——
 * 否则用户清空答案后反而会发布出旧答案。
 *
 * 🔒 不得在此加任何身份字段（`reviewer_id` 等）：审核人由后端从 JWT 取（红线 1）。
 */
export interface FaqReviewPayload {
  action: 'publish' | 'reject'
  question: string
  answer: string
}

/** 候选列表筛选。✅ 分页参数后端 `CandidateQuery` 已声明并生效 */
export interface CandidateQuery {
  status: CandidateStatus
  /** 置信度下限 [0,1] */
  min_confidence: number
  page: number
  page_size: number
}

/** 已发布 FAQ 查询（✅ 后端 `FaqQuery` 有 keyword / page / page_size） */
export interface FaqQuery {
  keyword: string
  page: number
  page_size: number
}

/** ✅ 缺口查询（be-09 已实现）。❌ 仍无 status 过滤 —— 清单定位是「近期热门缺口」，
 *  不是「按状态检索」；需要看已关闭的另开筛选，见 spec §9-6。 */
export interface GapQuery {
  min_frequency: number
  page: number
  page_size: number
}

/** ✅ 审核响应（be-08 已实现）：`{candidate_id, status, faq_id}` */
export interface CandidateReviewResult {
  candidate_id: string
  status: CandidateStatus
  /** 发布时产生的新 FAQ ID */
  faq_id?: string
}

/** ✅ 缓存开关响应（be-08 已实现）：`{faq_id, cache_enabled}` */
export interface FaqCacheResult {
  faq_id: string
  cache_enabled: boolean
}

/** ✅ 缺口转建响应（be-09 已实现）：`{gap_id, status, task_id}` */
export interface GapTaskResult {
  gap_id: string
  status: GapStatus
  /**
   * 生成的 `kb_import_task` ID。
   *
   * 🔴 **已由可选改为必回**（be-09 实现期定稿）：前端要拿它跳到导入中心，
   * 少了它只能给一句「操作成功」而用户不知道该干什么。
   * ⚠️ 它同时是**转建幂等键的一半** —— 后端复用已有任务时回的也是这个 id，
   * 所以「同一个缺口点两次」不会拿到两个不同的 task_id。
   */
  task_id: string
}

/** ✅ 挖掘响应（be-08 已实现）。`partial` 是**事后标注**，不是「被中断」 */
export interface MineResult {
  /** 本次新增候选数。后端不回时为空 → 前端退回泛化文案 */
  candidates_created?: number
  clusters_scanned?: number
}

/**
 * 顶部统计块（`GET /api/mining/stats/overview`）。
 *
 * ✅ **be-08 已实现**（spec §9-4 销账）：这 5 个字段名就是后端 `MiningOverviewDTO` 的字段名。
 *
 * 🔴 数值**全部投影自 `stats_service.get_overview()`**，后端**没有第二条聚合 SQL**。
 * 所以它的口径与 fe-05 的 `/api/stats/dashboard/overview` 必然一致 ——
 * 两处各写一份聚合会静默裂开（治理中心覆盖率 0.82、看板 0.79，双方都自洽、都不报错，
 * 排查者不知道该信哪个）。见 `P3-14` §4.2 与字典 §9.2 #140。
 *
 * ⚠️ 但**端点仍然是两个**：本端点的权限码是 `faq:candidate:list`
 * （`data_mining_router.py`），看板的是 `stats:dashboard:view`
 * （`stats_router.py`）—— 两者**不通用**，前端不能因为「数一样」就换用另一个端点：
 * 治理中心的用户未必有看板权限，换了会 403。
 */
export interface MiningOverviewVo {
  question_count: number
  unique_user_count: number
  /** 0–1 */
  faq_hit_rate: number
  /** 0–1 */
  kb_coverage: number
  avg_latency_ms: number
}
