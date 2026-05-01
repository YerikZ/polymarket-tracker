import { useEffect, useRef, useState } from "react";
import type { Alert } from "../lib/types";
import { wsUrl } from "../lib/api";

const MAX_FEED = 200;

export function useSignals() {
  const [signals, setSignals] = useState<Alert[]>([]);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const url = wsUrl("/ws/signals");
    let ws: WebSocket;
    let reconnectTimer: ReturnType<typeof setTimeout>;

    function connect() {
      ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data) as {
          type: "seed" | "new" | "update";
          data: Alert[];
        };
        if (msg.type === "seed") {
          setSignals(msg.data);
        } else if (msg.type === "new") {
          setSignals((prev) => [...msg.data, ...prev].slice(0, MAX_FEED));
        } else if (msg.type === "update") {
          // Merge copier_status/reason/spend into already-displayed rows
          const patchMap = new Map(msg.data.map((a) => [a.id, a]));
          setSignals((prev) =>
            prev.map((s) => (patchMap.has(s.id) ? { ...s, ...patchMap.get(s.id) } : s))
          );
        }
      };

      ws.onclose = () => {
        reconnectTimer = setTimeout(connect, 3_000);
      };
    }

    connect();
    return () => {
      ws?.close();
      clearTimeout(reconnectTimer);
    };
  }, []);

  return signals;
}
