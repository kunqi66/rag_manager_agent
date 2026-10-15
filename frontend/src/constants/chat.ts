/**
 * 问答工作台的常量与静态文案。
 *
 * 阈值、分隔符、上限一律集中在此，组件里不出现魔数（CLAUDE.md 代码规范）。
 * 文案取自 pencil 原型 `XA7T8` 的 `content` 原文，改动前先回原型确认。
 */

/** SSE 单帧的结束分隔符。sse-starlette 每帧以 `\n\n` 收尾 */
export const SSE_FRAME_SEPARATOR = '\n\n'

/**
 * 默认召回条数。
 * 与后端 `AskRequest.top_k` 的默认值（5）保持一致——前端**不自行调整**，
 * 否则看板（be-07）的召回统计口径会与用户实际提问不一致（fe-03 §8）。
 */
export const ASK_TOP_K = 5

/** 助手显示名。原型 `AiMsg/AiHead/AiName` 原文 */
export const ASSISTANT_NAME = '知识助手'

/** 输入区占位文案。原型 `Composer/ComposerPlaceholder` 原文 */
export const COMPOSER_PLACEHOLDER = '继续提问，例如「二线城市住宿费超标怎么办」'

/** 输入区快捷键提示。原型 `ComposerTools/ComposerHint` 原文 */
export const COMPOSER_HINT = 'Enter 发送 · Shift+Enter 换行'

/** 输入框自增高后的最大高度（px，约 6 行）。超过就内部滚动，别把消息流挤没 */
export const COMPOSER_MAX_HEIGHT = 132

/** 剩余字数少于总长的这个比例时才显示计数，避免一直挂个没用的 1000/1000 */
export const COMPOSER_COUNTER_THRESHOLD = 0.8

/** 流式中主按钮的文案（点击 = 中断当前生成） */
export const ACTION_STOP = '停止生成'

/** 发送按钮的 title（原型 `SendBtn` 是纯图标按钮，没有可见文字，靠 title 提供语义） */
export const ACTION_SEND = '发送'

/** 会话栏标题。原型 `SessionHead/SessionTitle` 原文 */
export const SESSION_LIST_TITLE = '会话记录'

/** 会话标题为空时的占位（`qa_session.title` 由后端按首问生成，可能尚未落库） */
export const SESSION_UNTITLED = '未命名会话'

/** 会话列表空态文案（原型未画，按 fe-03 §2 的「空态」职责补） */
export const SESSION_EMPTY_TEXT = '还没有会话记录'

/** 会话列表加载中的占位文案 */
export const LOADING_TEXT = '加载中…'

/** 删除会话的二次确认文案（spec §9-21：PRD 未指定确认交互，此处按惯例补） */
export const DELETE_CONFIRM_TITLE = '删除该会话？'
export const DELETE_CONFIRM_TEXT = '会话中的全部问答记录将一并删除，且不可恢复。'

/** 检索范围 chip 的固定前缀（原型 `ScopeChip/ScopeText` 原文） */
export const SCOPE_CHIP_PREFIX = '检索范围：'

/** 检索范围 chip 的固定后缀（原型原文，表示数据权限向下继承） */
export const SCOPE_CHIP_SUFFIX = '（含继承）'

/** 无部门归属时的等义兜底 —— 不依赖部门名也能说清语义 */
export const SCOPE_CHIP_FALLBACK = '检索范围：按你的数据权限（含部门继承）'

/**
 * 检索范围 chip 的文案。
 *
 * ✅ 2026-10-14 已还原原型：原型 `ScopeChip/ScopeText` 是「检索范围：财务部 · 成本组（含继承）」，
 * 用的是**部门名**（spec §9-7 曾因取不到部门名而退到等义表述）。
 * be-01 补 `dept_names` 后（spec §10-86），这里按原型拼回去。
 *
 * `deptLabel` 传 `stores/auth.ts` 的 `deptLabel` getter ——
 * **「取末两段拼 `·`」的规则在 store 里只实现一次**，与侧栏副标题（`NavSidebar.vue`）共用；
 * 本函数不自己 `slice`，否则两处会各自演化出「显示几级」的不同口径。
 * 无部门归属（空串）时退回 `SCOPE_CHIP_FALLBACK`。
 */
export function scopeChipText(deptLabel: string): string {
  return deptLabel ? `${SCOPE_CHIP_PREFIX}${deptLabel}${SCOPE_CHIP_SUFFIX}` : SCOPE_CHIP_FALLBACK
}

/**
 * 权限受限气泡文案。原型 `BlockedBubble/BlockedText` 原文（spec §9-9：与 fe-03 §4.4 的
 * 示例文案逐字不同，本页以原型为准）。`{count}` 由 `blocked.count` 填入。
 */
export const BLOCKED_TEXT_TEMPLATE = '本次检索已过滤 {count} 个无权限知识单元，按安全策略不展示其标题与正文。'

/** `ACL_REVEAL_BLOCKED_TITLES=true`（仅演示开关）时才出现的警示前缀 */
export const BLOCKED_DEMO_NOTICE = '演示模式：已开启受限标题回显'

/** 「毫秒级直出」徽标文案（`done.faq_hit === true`）。A7 场景二的可演示证据 */
export const FAQ_HIT_BADGE = '毫秒级直出'

/**
 * 原型 `AiMeta` 在「只出受限提示」时的文案。
 *
 * 判据是后端的 `done.llm_called === false`（be-05 §10-107 已冻结该字段）。
 * ⚠️ 历史轮次回放拿不到 `llm_called`，`AssistantBubble` 会退回 `token_out` 推断 ——
 *    两处口径见该组件的 `llmCalled` computed，不要在别处再写第三份判据。
 */
export const META_BLOCKED_NO_LLM = '受权限限制 · 未调用生成模型'

/** 各种流式中间态的元信息文案 */
export const META_STREAMING = '正在生成…'
export const META_STOPPED = '已停止生成'
export const META_INTERRUPTED = '生成中断'
export const META_FAILED = '生成失败'

/** 非正常结束时气泡上的动作文案（重试 = 用同样的问题重新发一条提问，不原地替换） */
export const ACTION_RETRY = '重试'

/**
 * 单个问题的长度上限。
 * ⚠️ PRD 未给数（fe-03 §6 只说「超长 → 400/1001」），此值是按常见量级定的（spec §9-22）。
 * 与后端实际约束对齐后回改。
 */
export const QUESTION_MAX_LENGTH = 1000

/** 联想词最多展示条数（接口未定义条数，前端截断，spec §9-15） */
export const SUGGESTION_LIMIT = 6

/** 引用列表超过该条数时收起为「共 N 条来源」，点开再展开 */
export const CITATION_COLLAPSE_THRESHOLD = 3

/** 引用列表的汇总行文案模板。原型无此行，按 fe-03 §2 的「含『共 N 条来源』折叠」补（spec §9-15 同类处置） */
export const CITATION_SUMMARY_TEMPLATE = '共 {count} 条来源'

/**
 * 引用卡片跳转到知识台账所需的功能权限码。
 *
 * ✅ **已冻结（2026-10-14，字典 §5.6.1 / §9 #11）**，与 `seed_org.py` 的 perm_14 逐字一致。
 *
 * ⚠️ 取的是 `kb:document:list`（**台账读权限**），不是 `KB_PERMS` 里的任何一个 ——
 * 能跳过去就意味着能看到全量台账。**`staff` 没有这个码**，故普通员工点引用卡片
 * **只读不跳**：这是刻意设计（台账按 PRD §3.3 不受数据权限约束，给了即违反红线 4），
 * 不是缺陷，别为了「让员工也能点」而放宽它。
 */
export const CITE_JUMP_PERMISSION = 'kb:document:list'

/** 引用卡片跳转的目标路由（带文档定位） */
export const CITE_JUMP_ROUTE = 'kb-documents'

/**
 * 引用卡片跳转时携带的**定位参数名**。
 *
 * 这是跨页契约（问答页 → 知识台账），**必须与 fe-02 / fe-05 用同一个名字**。
 * 曾用 `highlight`，与 fe-05 下钻用的 `document_id` 撞名——同一概念两个参数名，
 * 必然有一边静默失效。现统一为 `document_id`（fe-02 侧把 `highlight` 留作过渡别名，
 * 但本页不依赖别名）。
 */
export const CITE_JUMP_QUERY_KEY = 'document_id'

/**
 * 入站参数名：其它页跳进本页时用 `?q=<用户提问原文>` 预填输入框。
 *
 * 约定方是 fe-05 的「热门问题下钻」（`DashboardView` → `router.push({name:'chat', query:{q}})`）。
 * 消费规则见 `ChatView.vue`：**只填充不自动发送**，填完即 `router.replace` 抹掉该参数。
 */
export const PRESET_QUESTION_QUERY_KEY = 'q'

/**
 * `activeSessionId` 在 `sessionStorage` 的键。
 * 作用是刷新页面后能回到上次那个会话（fe-03 §4.2.6）。
 * **存的是会话 id，不是任何凭证**——token 仍然只走 `utils/token.ts`。
 */
export const ACTIVE_SESSION_KEY = 'kb_active_session'

/**
 * 空态文案（原型未画空态，按 fe-03 §2 的「空态」职责补，视觉用原型已有图元拼）。
 */
export const EMPTY_STATE = {
  title: '开始你的第一次提问',
  desc: '回答只基于你有权限访问的知识，引用逐条溯源。',
} as const
