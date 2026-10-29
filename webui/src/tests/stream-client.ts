import { BaboClient } from "@/lib/babo-client";
import type { InboundEvent } from "@/lib/types";

/** Real lifecycle and dispatch with an in-memory transport. */
export function createStreamClient() {
  const socket = {
    readyState: 1,
    onopen: null as (() => void) | null,
    onmessage: null as ((event: MessageEvent) => void) | null,
    send() {},
    close() {},
  };
  const client = new BaboClient({
    url: "ws://test", reconnect: false,
    socketFactory: () => socket as unknown as WebSocket,
  });
  client.connect();
  socket.onopen?.();
  return {
    client,
    receive(event: InboundEvent) {
      socket.onmessage?.({ data: JSON.stringify(event) } as MessageEvent);
    },
  };
}
