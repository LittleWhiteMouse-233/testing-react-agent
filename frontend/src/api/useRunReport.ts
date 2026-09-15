import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { $api } from "./client";
import type { StoredRunEvent } from "./contracts";
import { mergeStoredRunEvent, openRunEventStream } from "./runEvents";

export function useRunReport(runId: string) {
  const queryClient = useQueryClient();
  const report = $api.useQuery("get", "/api/runs/{test_run_id}", {
    params: { path: { test_run_id: runId } }
  });
  const [received, setReceived] = useState<StoredRunEvent[]>([]);
  const [connection, setConnection] = useState("connecting");
  const [contractError, setContractError] = useState(false);
  const reportRef = useRef(report);
  reportRef.current = report;
  const ready = !!report.data;
  const finished = report.data?.detail.run.status === "finished";

  useEffect(() => {
    if (!ready || finished) return;
    let disposed = false;
    const history = reportRef.current.data?.events ?? [];
    const source = openRunEventStream(runId, Math.max(0, ...history.map((event) => event.sequence)), {
      onEvent: (stored) => {
        if (disposed) return;
        setReceived((current) => mergeStoredRunEvent(current, stored));
        if (["run.started", "task.started", "task.finished", "tasks.skipped"].includes(stored.event.type)) {
          void reportRef.current.refetch();
        }
      },
      onTerminal: () => {
        if (disposed) return;
        void reportRef.current.refetch();
        void queryClient.invalidateQueries({ queryKey: ["get", "/api/runs"] });
        void queryClient.invalidateQueries({ queryKey: ["get", "/api/runs/statistics"] });
      },
      onContractError: () => { if (!disposed) setContractError(true); },
      onConnection: (state) => {
        if (disposed) return;
        setConnection(state);
        if (state === "connected") void reportRef.current.refetch();
      }
    });
    return () => { disposed = true; source.close(); };
  }, [runId, ready, finished, queryClient]);

  const events = useMemo(() => {
    const bySequence = new Map<number, StoredRunEvent>();
    for (const stored of [...(report.data?.events ?? []), ...received]) bySequence.set(stored.sequence, stored);
    return [...bySequence.values()].sort((a, b) => a.sequence - b.sequence);
  }, [report.data?.events, received]);
  return { ...report, events, connection: finished ? "finished" : connection, contractError };
}
