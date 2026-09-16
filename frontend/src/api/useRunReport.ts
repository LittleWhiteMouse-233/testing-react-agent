import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { $api } from "./client";
import type { StoredRunEvent } from "./contracts";
import { mergeStoredRunEvents, openRunEventStream } from "./runEvents";

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
    const coveredSequence = report.data?.events.at(-1)?.sequence ?? 0;
    setReceived((current) => {
      const remaining = current.filter((event) => event.sequence > coveredSequence);
      return remaining.length === current.length ? current : remaining;
    });
  }, [report.data?.events]);

  useEffect(() => {
    if (!ready || finished) return;
    let disposed = false;
    let reconnecting = false;
    const history = reportRef.current.data?.events ?? [];
    const source = openRunEventStream(runId, history.at(-1)?.sequence ?? 0, {
      onEvent: (stored) => {
        if (disposed) return;
        if (stored.sequence > (reportRef.current.data?.events.at(-1)?.sequence ?? 0)) {
          setReceived((current) => mergeStoredRunEvents(current, [stored]));
        }
        if (["run.started", "task.started", "task.finished", "tasks.skipped"].includes(stored.event.type)) {
          void reportRef.current.refetch();
        }
      },
      onTerminal: () => {
        if (disposed) return;
        setConnection("awaiting_result");
        void reportRef.current.refetch();
        void queryClient.invalidateQueries({ queryKey: ["get", "/api/runs"] });
        void queryClient.invalidateQueries({ queryKey: ["get", "/api/runs/statistics"] });
      },
      onContractError: () => { if (!disposed) setContractError(true); },
      onConnection: (state) => {
        if (disposed) return;
        setConnection(state);
        if (state === "connected" && reconnecting) void reportRef.current.refetch();
        reconnecting = state === "reconnecting";
      }
    });
    return () => { disposed = true; source.close(); };
  }, [runId, ready, finished, queryClient]);

  const events = useMemo(() => mergeStoredRunEvents(report.data?.events ?? [], received), [report.data?.events, received]);
  return { ...report, events, connection: finished ? "finished" : connection,
    awaitingFinalResult: !finished && connection === "awaiting_result", contractError };
}
