import { useEffect, useRef, useCallback } from 'react';
import { WS_URL, WS_RECONNECT_INTERVAL } from '../utils/constants';
import { useRequestStore } from '../stores/requestStore';
import { useConfigStore } from '../stores/configStore';
import type { WebSocketEvent } from '../types/websocket';

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout>>(undefined);
  const handleEvent = useRequestStore((s) => s.handleWebSocketEvent);
  const setConnectionStatus = useConfigStore((s) => s.setConnectionStatus);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    setConnectionStatus('connecting');
    const ws = new WebSocket(WS_URL);

    ws.onopen = () => setConnectionStatus('connected');

    ws.onclose = () => {
      setConnectionStatus('disconnected');
      reconnectTimerRef.current = setTimeout(connect, WS_RECONNECT_INTERVAL);
    };

    ws.onerror = () => {
      ws.close();
    };

    ws.onmessage = (event) => {
      try {
        const data: WebSocketEvent = JSON.parse(event.data);
        if (data.type && data.type !== 'connected') {
          handleEvent(data);
        }
      } catch {
        /* malformed message */
      }
    };

    wsRef.current = ws;
  }, [handleEvent, setConnectionStatus]);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectTimerRef.current);
      wsRef.current?.close();
    };
  }, [connect]);
}
