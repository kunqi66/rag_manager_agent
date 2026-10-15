import { getCurrentScope, onScopeDispose, ref } from 'vue'
import type { Ref } from 'vue'

import {
  IMPORT_POLL_INTERVAL_MS,
  IMPORT_POLL_MAX_DURATION_MS,
  IMPORT_POLL_MAX_FAILURES,
} from '@/constants/kb'
import { useKbDocumentStore } from '@/stores/kbDocument'

/**
 * 导入任务轮询（fe-02 §4.3 ①）。
 *
 * **本页最容易写错的一环**，所以单独立成 composable 而不是塞进 `ImportDrawer`：
 * 轮询的启停生命周期（全部终态停 / 抽屉关闭停 / 路由离开停 / 组件卸载停）
 * 与抽屉的显隐是两个正交问题，混在一起会让 `onUnmounted` 里塞满副作用，
 * 而漏掉任一个出口都会留下**永久定时器**——每 2s 打一次接口，用户切到别的页面毫无察觉。
 *
 * 本 composable 只负责「什么时候打、什么时候停」，**不弹任何 UI**：
 * 提示文案交给调用方（`onSettled` / `onTimeout` / `onError` 回调），便于复用与测试。
 */
export interface UseImportPollingOptions {
  /** 全部任务到达终态时回调（调用方在此节流重拉台账） */
  onSettled?: () => void
  /** 达到轮询上限仍在跑时回调（任务疑似卡死） */
  onTimeout?: () => void
  /** 连续失败超过容忍次数时回调 */
  onError?: (consecutiveFailures: number) => void
}

export interface UseImportPollingReturn {
  /** 是否正在轮询（UI 可据此显示「实时更新中」） */
  running: Ref<boolean>
  start: () => void
  stop: () => void
}

export function useImportPolling(options: UseImportPollingOptions = {}): UseImportPollingReturn {
  const store = useKbDocumentStore()
  const running = ref(false)

  let timer: ReturnType<typeof setInterval> | null = null
  let startedAt = 0
  let consecutiveFailures = 0
  /** 上一轮还没回来就跳过本轮，避免慢接口把请求堆成一串 */
  let inFlight = false

  function stop(): void {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
    running.value = false
    consecutiveFailures = 0
  }

  async function tick(): Promise<void> {
    if (inFlight) return
    inFlight = true
    try {
      await store.fetchTasks()
      consecutiveFailures = 0

      // ① 停止条件：全部终态
      if (store.allTasksSettled) {
        stop()
        options.onSettled?.()
        return
      }

      // ② 停止条件：超过上限。不设上限就是无限轮询——后端 `celery_app.include`
      //    漏注册会让任务永远 pending，静默失败在前端只表现为「进度条永远转圈」。
      if (Date.now() - startedAt >= IMPORT_POLL_MAX_DURATION_MS) {
        stop()
        options.onTimeout?.()
      }
    } catch {
      // 网络抖动不终止轮询：单次失败静默重试（fe-02 §6 末行）
      consecutiveFailures += 1
      if (consecutiveFailures >= IMPORT_POLL_MAX_FAILURES) {
        stop()
        options.onError?.(consecutiveFailures)
      }
    } finally {
      inFlight = false
    }
  }

  function start(): void {
    stop() // 幂等：重复 start 不会叠出第二个定时器
    running.value = true
    startedAt = Date.now()
    consecutiveFailures = 0
    // 立刻打一次，不等第一个 2s —— 用户点完「开始导入」要马上看到任务行
    void tick()
    timer = setInterval(() => {
      void tick()
    }, IMPORT_POLL_INTERVAL_MS)
  }

  // 组件卸载兜底。放在 composable 里而非页面里，是为了让「用了轮询就一定会清理」
  // 成为默认行为——靠调用方记得写 onUnmounted 迟早会漏。
  if (getCurrentScope()) onScopeDispose(stop)

  return { running, start, stop }
}
