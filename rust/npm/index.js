import init, { WebTerminal } from "./wasm/hrns_tui.js";
import { attachTerminal } from "./terminal.js";

let ready;

export async function mountTerminal(container, options = {}) {
  options.signal?.throwIfAborted();
  ready ??= init().catch((error) => {
    ready = undefined;
    throw error;
  });
  await ready;
  return attachTerminal(WebTerminal, container, options);
}
