import { BarChart, PieChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'

/**
 * ECharts 的**按需注册**与**令牌取色**。本页所有图表都从这里进。
 *
 * 1. **按需引入**：全量 `import * as echarts from 'echarts'` 会把整个库（约 1MB+）打进产物。
 *    `use([...])` 缺任何一个组件，对应的配置项会被 ECharts **静默忽略**
 *    （图能出来但没网格线 / 没提示框），所以这份清单必须与**实际用到的配置**逐项对齐。
 *
 *    ⚠️ **清单里只留真正用到的**：本页只有柱状（趋势、延时）与环形（来源构成）两种图；
 *    图例是**自绘的 HTML**（原型是 8×8 圆点，ECharts 自带的方块对不上），
 *    所以 `LegendComponent` 与 `LineChart` 都**不注册** —— 「以后可能用得上」不是注册的理由，
 *    那等于放弃了按需引入的意义。
 * 2. **令牌取色**：ECharts 的 `color` 收的是**真实色值**，**不吃 CSS 变量**。
 *    直接写 `'#2f6fed'` 会破坏「全项目只有 tokens.css 有十六进制」这条硬约束，
 *    所以统一走 `token('--kb-brand')` —— 配置里出现的仍是令牌名，取色时机在运行时。
 *
 * 只 `use` 一次：本模块被多个图表组件 import，但 ES 模块只求值一次。
 */
echarts.use([BarChart, PieChart, GridComponent, TooltipComponent, CanvasRenderer])

export { echarts }

/** ECharts 实例类型。用 `ReturnType` 取，避免去猜 `echarts/core` 导出的类型名。 */
export type ChartInstance = ReturnType<typeof echarts.init>

/**
 * 图表配置的类型。**type-only 再导出**：`import type` 在编译后被抹掉，
 * 不会把整个 `echarts` 主包的内容拉进产物（真正的运行时依赖仍是 `echarts/core` + `use([...])`）。
 */
export type { EChartsOption } from 'echarts'

/**
 * 读设计令牌的真实色值。
 *
 * 传令牌名（如 `'--kb-brand'`），返回 `#2f6fed`。
 * 容器未挂载 / SSR 时 `document` 不可用——本页是纯客户端渲染，且调用点都在 `onMounted` 之后。
 */
export function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

/**
 * 提示框样式的**具体**类型。
 *
 * 刻意不写成 `NonNullable<EChartsOption['tooltip']>`：那是个联合类型（对象 | 数组），
 * 展开到 option 里时 TS 会把它按联合分发，得到的对象未必能通过后续的赋值检查。
 * 这里给一个确定的对象形状，展开后类型稳定。
 */
export interface TooltipStyle {
  backgroundColor: string
  borderColor: string
  borderWidth: number
  textStyle: { color: string; fontSize: number }
  extraCssText: string
}

/**
 * 三个图表共用的提示框样式，避免各写一份导致弹层长得不一样。
 */
export function tooltipDefaults(): TooltipStyle {
  return {
    backgroundColor: token('--kb-bg-card'),
    borderColor: token('--kb-border'),
    borderWidth: 1,
    textStyle: { color: token('--kb-text-primary'), fontSize: 12 },
    // 投影也走令牌：`extraCssText` 是拼给浏览器的 CSS 字符串，同样不吃 `var()`，
    // 在这里写字面量等于往 TS 文件里塞颜色 —— 与令牌规则同一处置（见 tokens.css 的 --kb-shadow-tooltip）
    extraCssText: `box-shadow: ${token('--kb-shadow-tooltip')}; border-radius: 8px;`,
  }
}

/**
 * 坐标轴刻度标签的统一样式。
 */
export function axisLabelColor(): string {
  return token('--kb-text-muted')
}

/**
 * 坐标轴分格线的统一样式（浅色虚线，别用实线抢主视觉）。
 */
export function splitLineStyle(): { lineStyle: { color: string; type: 'dashed' } } {
  return { lineStyle: { color: token('--kb-border-soft'), type: 'dashed' } }
}
