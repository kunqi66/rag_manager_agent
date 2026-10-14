# 后端模块 Spec：{模块名}

> 状态：{草稿|评审|冻结|实现中|已实现} ｜ 期次：{P1|P2|P3} ｜ 关联验收：{A1–A9}

## 1. 模块定位

- 所属层：{api / engine / service / client / task}
- 一句话职责：
- 边界（做什么 / 明确不做什么）：

## 2. 分层落点

| 层 | 文件 | 内容 |
|---|---|---|
| api | `backend/api/xxx_router.py` | 路由：只校验 + 注入 + 转发 |
| engine/service | `backend/engines/xxx` 或 `backend/service/` | 业务编排 |
| client | `backend/clients/` | 外部依赖封装（如需） |
| models | `backend/models/` | ORM |
| schemas | `backend/schemas/` | Pydantic DTO |
| tasks | `backend/tasks/` | Celery 任务（如需） |

## 3. 涉及的表（引用 PRD §7.2）

| 表 | 用途 | 关键字段 |

## 4. API 契约（引用 PRD §8，逐条列）

| 方法 | 路径 | 请求 schema | 响应 schema | 功能权限 | 数据权限 |

## 5. 核心逻辑

- 关键算法 / 机制：{如混合检索表达式、部门向下继承计算、切片策略}
- 涉及的安全红线：{逐条列出编号 + 本模块如何满足}
- 涉及的外部依赖：{MinerU / BGE / DeepSeek / Milvus / Redis}

## 6. 异常与错误码

| 场景 | 错误码 | HTTP 状态 | message |

## 7. 异步任务（如有）

| 任务 | 触发方式 | 重试策略 | 超时 |

## 8. 验收标准

- [ ] {逐条可测的标准，对应 PRD 验收编号}

## 9. 测试数据（引用 PRD §9 场景）

- {需要的造数 / 场景}

## 10. 待确认

- {查不到的信息，列成需要用户答复的问题}
