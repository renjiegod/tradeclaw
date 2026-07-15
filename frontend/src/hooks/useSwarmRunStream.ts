import { useEffect, useRef, useState } from "react";

import { getSwarmRun, swarmRunEventStreamUrl } from "../api";
import type { SwarmRun, SwarmRunStatus, SwarmWorkerStatus } from "../types";

/** swarm 事件类型 → 它把目标 worker 推进到的状态。 */
const EVENT_TO_STATUS: Record<string, SwarmWorkerStatus> = {
  task_started: "in_progress",
  task_retry: "in_progress",
  task_completed: "completed",
  task_failed: "failed",
  task_blocked: "blocked",
};

type WorkerMap = Record<string, SwarmWorkerStatus>;

/**
 * 订阅一个 swarm run 的 SSE 事件流，维护「任务 id → 实时状态」映射。
 *
 * 仿 AssistantPage 的原生 EventSource 用法：组件卸载或 runId 变化时 close，
 * 浏览器原生处理重连；用 ``last_event_id`` 续上（首帧拉取 run 现状做初值）。
 */
export function useSwarmRunStream(runId: string | null) {
  const [run, setRun] = useState<SwarmRun | null>(null);
  const [workerStatus, setWorkerStatus] = useState<WorkerMap>({});
  const [runStatus, setRunStatus] = useState<SwarmRunStatus | null>(null);
  const streamRef = useRef<EventSource | null>(null);
  const lastEventIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!runId) {
      setRun(null);
      setWorkerStatus({});
      setRunStatus(null);
      return;
    }

    let closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      streamRef.current?.close();
      streamRef.current = null;
    };

    // 先拉一次现状作为初值（重连/历史回放时已结束的卡片也能复原）。
    void getSwarmRun(runId)
      .then((fresh) => {
        if (closed) return;
        setRun(fresh);
        setRunStatus(fresh.status);
        setWorkerStatus(
          Object.fromEntries(fresh.tasks.map((t) => [t.task_id, t.status])),
        );
      })
      .catch(() => {});

    const stream = new EventSource(swarmRunEventStreamUrl(runId, lastEventIdRef.current));
    streamRef.current = stream;

    const applyTaskEvent = (eventType: string) => (rawEvent: Event) => {
      try {
        const ev = rawEvent as MessageEvent;
        if (typeof ev.lastEventId === "string" && ev.lastEventId) {
          lastEventIdRef.current = ev.lastEventId;
        }
        const payload = JSON.parse(ev.data) as Record<string, unknown>;
        const taskId = typeof payload.task_id === "string" ? payload.task_id : "";
        const next = EVENT_TO_STATUS[eventType];
        if (taskId && next) {
          setWorkerStatus((prev) => ({ ...prev, [taskId]: next }));
        }
      } catch {
        // 忽略格式错误的事件 payload。
      }
    };

    for (const eventType of Object.keys(EVENT_TO_STATUS)) {
      stream.addEventListener(eventType, applyTaskEvent(eventType));
    }

    const onRunDone = () => {
      // run 结束：重新拉取完整结果（含 summary / final_report），并关闭流。
      void getSwarmRun(runId)
        .then((fresh) => {
          if (closed) return;
          setRun(fresh);
          setRunStatus(fresh.status);
          setWorkerStatus(
            Object.fromEntries(fresh.tasks.map((t) => [t.task_id, t.status])),
          );
        })
        .catch(() => {})
        .finally(() => close());
    };
    stream.addEventListener("run_completed", onRunDone);
    stream.addEventListener("run_error", onRunDone);

    return close;
  }, [runId]);

  return { run, workerStatus, runStatus };
}
