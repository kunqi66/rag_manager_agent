/**
 * 治理中心（fe-06）的常量与文案。
 *
 * 这里集中三样东西，都是「散在组件里迟早会分叉」的：
 * 1. 权限码 —— 后端定稿后**只改这一处**（见 P3-06-fe-governance spec §9-13）；
 * 2. 阈值 —— 后端配置的镜像值，必须与 `settings.py` 同步（spec §9-18）；
 * 3. 逐字文案 —— 验收要照抄的提示语（发布 Toast、缓存 tooltip、转建措辞）。
 *
 * 注意 `FaqCandidateItem` 等类型是**结构**，不放这里；文案与阈值才放这里。
 */

import type { CandidateStatus, GapStatus } from '@/types/governance'

/**
 * 功能权限码。
 *
 * 🔒 **`v-permission` 只是显隐体验，不是安全边界**（CLAUDE.md 红线 7）：
 * 这些码只决定按钮看不看得见，后端每个写接口仍必须独立 `require_permission`——
 * 手工调 `POST /api/mining/faq/candidates/{id}/review` 无权限账号必须得 403。
 *
 * `faq:*` 五条已由 be-08 于 2026-10-14 冻结（字典 §5.6.4 / §9 #61、#78），**与后端种子逐字一致**。
 * 🔴 `CANDIDATE_PUBLISH` 的码名是**拍板结果**：PRD §3.2 逐字写了 `faq:candidate:publish`，
 *    字典 #78 曾建议改叫 `faq:candidate:review`，**不采纳**（改名就是"按钮静默不显示"的形状）。
 * `gap:*` 两条已由 be-09 于 2026-10-14 冻结（字典 §5.6.5 / §9 #68），**与后端种子逐字一致**。
 */
export const PERM = {
  /** ✅ 已冻结。覆盖「发布」与「忽略」两个动作（同一端点、同一码） */
  CANDIDATE_PUBLISH: 'faq:candidate:publish',
  /** ✅ 已冻结（be-08 / 字典 §5.6.4） */
  CACHE_UPDATE: 'faq:cache:update',
  /** ✅ 已冻结（be-09 / 字典 §5.6.5）。写接口，后端独立校验（红线 7） */
  GAP_TASK_CREATE: 'gap:task:create',
  /** ✅ 已冻结（be-09 / 字典 §5.6.5）。缺口清单含**别人部门**的提问原文，非"登录即可" */
  GAP_LIST: 'gap:list',
  /** ✅ 已冻结（be-08 / 字典 §5.6.4） */
  CANDIDATE_MINE: 'faq:candidate:mine',
  /**
   * ✅ 已冻结（be-08 / 字典 §5.6.4）。
   * 🔴 与 `kb:*` 不同：**读接口也挂了码**。候选列表含**别人部门**的提问原文
   *    （聚类窗口是全局的），不是"登录即可"能看的。
   */
  CANDIDATE_LIST: 'faq:candidate:list',
  /** ✅ 已冻结。已发布 FAQ 台账（含 `answer` 正文，同 `CANDIDATE_LIST` 的理由） */
  FAQ_LIST: 'faq:list',
} as const

/** 列表默认页大小，与后端 `GET /faq`、`GET /gaps` 的 `page_size` 默认值 20 一致 */
export const DEFAULT_PAGE_SIZE = 20

/**
 * 聚类最小频次。**这是后端 `FAQ_MIN_FREQUENCY=5` 的镜像值**（be-08 §5.4）。
 * 前端展示卡片的提示文案、并在触发挖掘时显式带上同一个值，避免两边不一致。
 * 🟡 后端改了默认值必须同步这里（spec §9-18）。
 */
export const FAQ_MIN_FREQUENCY = 5

/** 缺口频次下限默认值，与后端 `GET /gaps` 的 `min_frequency=1` 一致 */
export const GAP_MIN_FREQUENCY = 1

// ---------------------------------------------------------------------------
// 状态文案与配色
// ---------------------------------------------------------------------------

/** 胶囊色调。组件按 tone 映射到 --kb-* 令牌，避免在组件里写颜色分支 */
export type PillTone = 'brand' | 'success' | 'warning' | 'danger' | 'muted'

export const CANDIDATE_STATUS_META: Record<CandidateStatus, { label: string; tone: PillTone }> = {
  pending: { label: '待审核', tone: 'warning' },
  published: { label: '已发布', tone: 'success' },
  rejected: { label: '已驳回', tone: 'muted' },
}

/**
 * 缺口状态文案。
 * ⚠️ `tasked` 一律写「已建补充任务」而**不是**「已解决 / 处理中」——
 * 转建只创建任务，缺口要变 `closed` 得靠补料后**再次提问命中**（be-09 §4.4）。
 * 写成「已解决」会让验收 A8 的闭环被误判为已完成，这是 fe-06 §4.3③.4 点名的坑。
 */
export const GAP_STATUS_META: Record<GapStatus, { label: string; tone: PillTone }> = {
  open: { label: '待补料', tone: 'danger' },
  tasked: { label: '已建补充任务', tone: 'warning' },
  closed: { label: '已关闭', tone: 'success' },
}

// ---------------------------------------------------------------------------
// 逐字文案（验收可演示点，不要改写）
// ---------------------------------------------------------------------------

/** 发布成功提示 —— 直接对应 A7 与 PRD §6.3.2，把「发布」这个动作的业务价值说清楚 */
export const PUBLISH_SUCCESS_TOAST =
  '已发布并写入高速缓存，后续同义提问将毫秒级直出，不再调用生成模型'

/**
 * 缓存开关 tooltip —— 不说清的话管理员会以为「关闭 = 删除」。
 * 关闭后该 FAQ 仍在库中，只是不再参与进程内索引的匹配（PRD §6.3.2）。
 */
export const CACHE_SWITCH_TOOLTIP =
  '关闭后该 FAQ 仍在知识库中，但不再参与进程内索引的毫秒级匹配，同义提问将回到常规检索链路。'

/** 待审核候选卡片的副标题提示，阈值见 FAQ_MIN_FREQUENCY */
export const CANDIDATE_CARD_HINT = `聚类阈值 ≥ ${FAQ_MIN_FREQUENCY} 次`

/** 转建成功提示。措辞必须是「创建补充任务」而非「关闭缺口」（fe-06 §4.3③.4） */
export const GAP_TASK_SUCCESS_TOAST = '已创建知识补充任务，请上传补料文档完成闭环'

/**
 * 转建前的「分类确认」弹框标题与输入占位。
 *
 * ⚠️ **这两个串没有逐字出处**（spec §9-23）：fe-06 §4.3③.3 只写到
 * 「先弹出**分类确认**（预填 `suggested_category`，可改——`POST` 接口有 `category` Query 参数）」，
 * 没给标题与占位文案；Tab3 在原型 `VOwfE` 里**根本没画**，更无从抄。
 * 这里按设计的语义起名：标题点明「这是在确认分类」，占位说明「可留空」
 * （be-09 §4.3：`category` 空则沿用缺口的 `suggested_category`）。
 * **产品定稿后改这两行即可**，别去改调用点。
 */
export const GAP_TASK_PROMPT_TITLE = '确认补充任务分类'
export const GAP_TASK_PROMPT_PLACEHOLDER = '建议分类（可留空）'

/**
 * 缺口清单空态。
 * ⚠️ 必须带上后半句：背后是 §9-20 的已知缺陷——`mining.aggregate_knowledge_gaps`
 * **没有进 beat**，缺口池恒空。不写清楚的话「页面做完了但没数据」会被当成前端 bug。
 */
export const GAP_EMPTY_TEXT = '暂无知识缺口（缺口聚合任务需已被调度）'

/** 已发布 FAQ 空态。关键词搜不到时也走这条 */
export const FAQ_EMPTY_TEXT = '暂无已发布 FAQ'

/** 挖掘完成提示（后端未回新增数时的泛化文案，见 spec §9-8） */
export const MINE_DONE_TEXT = '挖掘完成，请查看候选列表'
export const MINE_EMPTY_TEXT = '窗口内没有可挖掘的提问'
/** 后端回了新增数时用；本页当前按「不回」处理，仅保留模板 */
export const mineDoneText = (count: number): string => `本次挖掘新增 ${count} 条候选`

/**
 * 挖掘被前端超时中断时的定向提示（spec §9-1）。
 * `http.ts` 全局 timeout 15s，而挖矿是同步慢接口——分不清的话会被误判为后端故障。
 */
export const MINE_TIMEOUT_TEXT =
  '聚类请求超过前端 15 秒上限被中断。请确认后端是否已改为异步接口（见 spec §9-1）。'

/** 离开页面 / 切换候选时的未保存提示。润色是人工劳动，丢失代价高（fe-06 §4.3①.7） */
export const DIRTY_CONFIRM_TEXT = '有未保存的修改，切换后当前编辑内容将丢失。确定继续吗？'
export const DIRTY_CONFIRM_TITLE = '未保存的修改'

/** 发布前的轻确认。发布会写缓存、直接影响线上回答，值得一次确认（fe-06 §4.3①.6） */
export const PUBLISH_CONFIRM_TITLE = '确认发布'
export const PUBLISH_CONFIRM_TEXT =
  '发布后该问答对会写入高速缓存，同义提问将直接命中、不再调用生成模型。确定发布吗？'

/** 驳回是低风险可逆动作，**不做二次确认**（fe-06 §4.3①.6） */
export const REJECT_SUCCESS_TOAST = '已忽略该候选'

/**
 * 「手动触发聚类」按钮的说明。
 * 不标注的话管理员会以为必须天天手点——常态其实是 beat 每日 02:00 自动跑（PRD §6.3.1）。
 */
export const MINE_BUTTON_TOOLTIP =
  '仅供演示与手动补跑。常态由定时任务每日 02:00 自动聚类，无需手动触发。'

/** 转建成功后的二次确认框：把「去上传补料文档」做成确认按钮，避免用富文本 Message 塞按钮 */
export const GAP_TASK_DONE_TITLE = '补充任务已创建'
export const GAP_TASK_DONE_CONFIRM = '去上传补料文档'
export const GAP_TASK_DONE_CANCEL = '稍后再说'

/** 挖掘耗时提示（同步慢接口期间暴露给用户的文案，fe-06 §6） */
export const MINE_RUNNING_TEXT = '正在聚类，请稍候'

// ---------------------------------------------------------------------------
// 错误提示（fe-06 §6 的错误码映射）
// ---------------------------------------------------------------------------

/** 403 / code 2002 */
export const ERROR_NO_PERMISSION = '无操作权限，请联系管理员'
/** 404/409 / code 3001/3002 —— 并发审核是真实场景（多个管理员同时作业） */
export const ERROR_STALE_RECORD = '该记录已被他人处理，列表已刷新'
/** 兜底 */
export const ERROR_FALLBACK = '操作失败，请稍后重试'

/** 跳 fe-02 导入中心的 query 键。🟡 需与 fe-02 对齐（spec §9-21） */
export const OPEN_IMPORT_QUERY_KEY = 'openImport'
export const OPEN_IMPORT_QUERY_VALUE = '1'
