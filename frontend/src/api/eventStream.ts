export interface EventStreamSource {
  close: () => void;
  onmessage: ((event: MessageEvent<string>) => void) | null;
  onopen?: ((event: Event) => void) | null;
  onerror?: ((event: Event) => void) | null;
}

export function openEventStream<T, Source extends EventStreamSource>(
  url: string,
  parse: (raw: string) => T,
  callbacks: {
    onEvent: (event: T) => void;
    onContractError: (error: unknown) => void;
    onConnection?: (state: "connected" | "reconnecting") => void;
  },
  createSource: (url: string) => Source
): Source {
  const source = createSource(url);
  source.onopen = () => callbacks.onConnection?.("connected");
  source.onerror = () => callbacks.onConnection?.("reconnecting");
  source.onmessage = (raw) => {
    let event: T;
    try {
      event = parse(raw.data);
    } catch (error) {
      source.close();
      callbacks.onContractError(error);
      return;
    }
    callbacks.onEvent(event);
  };
  return source;
}
