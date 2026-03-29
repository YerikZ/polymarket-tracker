import { useEffect, useRef, useState } from "react";
import type { Alert } from "../lib/types";

const MAX_FEED = 200;

export function useSignals() {
  const [signals, setSignals] = useState<Alert[]>([]);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/ws/signals`;
    let ws: WebSocket;
    let reconnectTimer: ReturnType<typeof setTimeout>;

    function connect() {
      ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data) as { type: "seed" | "new"; data: Alert[] };
        if (msg.type === "seed") {
          setSignals(msg.data);
        } else {
          setSignals((prev) => {
            const next = [...msg.data, ...prev];
            return next.slice(0, MAX_FEED);
          });
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
