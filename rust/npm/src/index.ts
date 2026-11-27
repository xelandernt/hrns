import init, { WebTerminal } from "../wasm/hrns_tui.js";
import { attachTerminal } from "./terminal.js";
import type { MountedTerminal, TerminalOptions } from "./terminal.js";

export type { MountedTerminal, TerminalOptions } from "./terminal.js";

let ready: ReturnType<typeof init> | undefined;

export async function mountTerminal(
  container: HTMLElement,
  options: TerminalOptions = {},
): Promise<MountedTerminal> {
  options.signal?.throwIfAborted();
  ready ??= init().catch((error) => {
    ready = undefined;
    throw error;
  });
  await ready;
  return attachTerminal(WebTerminal, container, options);
}
