# hrns browser terminal

`@xelandernt/hrns-tui` embeds the hrns terminal in a browser app.

```sh
npm install @xelandernt/hrns-tui
```

## React

Give the container a height. The canvas follows the container's size. Call
`focus()` to move keyboard focus to the terminal programmatically. Press Escape, then Tab, to move keyboard focus back
to the host page.

```tsx
import { useEffect, useRef, useState } from "react";
import { mountTerminal } from "@xelandernt/hrns-tui";

export function HarnessTerminal({ endpoint }: { endpoint: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string>();

  useEffect(() => {
    if (!host.current) return;
    const controller = new AbortController();
    setError(undefined);
    mountTerminal(host.current, {
      endpoint,
      cwd: "/workspace",
      signal: controller.signal,
    }).catch((error: Error) => {
      if (!controller.signal.aborted) setError(error.message);
    });
    return () => controller.abort();
  }, [endpoint]);

  return (
    <>
      {error && <p role="alert">{error}</p>}
      <div ref={host} style={{ height: "32rem" }} />
    </>
  );
}
```

The returned handle also has an idempotent `dispose()` method. The abort signal
covers cleanup during Wasm loading and after mounting.

For Vite, exclude the package from dependency pre-bundling so its Wasm URL
resolves alongside the generated JavaScript in development.

```js
export default {
  optimizeDeps: { exclude: ["@xelandernt/hrns-tui"] },
};
```

Omit `endpoint` to connect to `/acp/agent` on the page origin. To connect to a
different host, pass an absolute `wss://` URL and configure your server to accept
the page's origin. Loopback development also supports `ws://`. The `cwd`
option is a path on the remote server. Use `session` to resume a saved session.
