export interface TerminalOptions {
  /** ACP WebSocket URL. Defaults to /acp/agent on the current origin. */
  endpoint?: string;
  /** Absolute workspace path on the ACP server. */
  cwd?: string;
  /** ID of an existing ACP session to resume. */
  session?: string;
  theme?: "hrns" | "textual-dark" | "textual-light" | "ansi-dark" | "ansi-light";
  /** Aborting cancels a pending mount or disposes a mounted terminal. */
  signal?: AbortSignal;
}

export interface MountedTerminal {
  focus(): void;
  dispose(): void;
}

export function mountTerminal(
  container: HTMLElement,
  options?: TerminalOptions,
): Promise<MountedTerminal>;
