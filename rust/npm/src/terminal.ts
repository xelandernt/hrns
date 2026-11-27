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

interface Terminal {
  draw(): void;
  resize(): void;
  free(): void;
}

type TerminalConstructor = new (
  root: HTMLElement,
  input: HTMLInputElement,
  endpoint?: string,
  cwd?: string,
  session?: string,
  theme?: string,
) => Terminal;

let nextTerminalId = 0;

export function attachTerminal(
  WebTerminal: TerminalConstructor,
  container: HTMLElement,
  options: TerminalOptions = {},
): MountedTerminal {
  const { endpoint, cwd, session, theme, signal } = options;
  signal?.throwIfAborted();

  const doc = container.ownerDocument;
  const view = doc.defaultView;
  if (doc !== globalThis.document || !view || !container.isConnected) {
    throw new Error("Mount hrns in a connected element in the current document");
  }
  const root = doc.createElement("div");
  do {
    root.id = `hrns-terminal-${++nextTerminalId}`;
  } while (doc.getElementById(root.id));
  root.tabIndex = 0;
  root.setAttribute("role", "region");
  root.setAttribute("aria-label", "hrns terminal");
  root.setAttribute("aria-description", "Press Escape, then Tab, to leave the terminal.");
  Object.assign(root.style, {
    width: "100%",
    height: "100%",
    overflow: "hidden",
    backgroundColor: "#121212",
  });

  const input = doc.createElement("input");
  input.type = "file";
  input.hidden = true;
  input.setAttribute("aria-label", "Attach a file");
  root.append(input);
  container.append(root);

  let terminal: Terminal | undefined;
  let observer: ResizeObserver | undefined;
  let frame: number | undefined;
  let resize = false;
  let disposed = false;
  let escapePressed = false;

  function allowTabOut(event: KeyboardEvent) {
    if (event.key === "Shift") return;
    const leaving = escapePressed && event.key === "Tab";
    escapePressed = event.key === "Escape" && !event.isComposing;
    if (leaving) event.stopImmediatePropagation();
  }

  function resetEscape() {
    escapePressed = false;
  }

  const dispose = () => {
    if (disposed) return;
    disposed = true;
    signal?.removeEventListener("abort", dispose);
    root.removeEventListener("keydown", allowTabOut, true);
    root.removeEventListener("blur", resetEscape);
    if (frame !== undefined) view.cancelAnimationFrame(frame);
    observer?.disconnect();
    try {
      terminal?.free();
    } finally {
      root.remove();
    }
  };

  try {
    root.addEventListener("keydown", allowTabOut, true);
    root.addEventListener("blur", resetEscape);
    const mounted = new WebTerminal(root, input, endpoint, cwd, session, theme);
    terminal = mounted;
    observer = new view.ResizeObserver(() => { resize = true; });
    observer.observe(root);
    signal?.addEventListener("abort", dispose, { once: true });

    const draw = () => {
      try {
        if (resize) {
          resize = false;
          mounted.resize();
        }
        mounted.draw();
        frame = view.requestAnimationFrame(draw);
      } catch (error) {
        dispose();
        throw error;
      }
    };

    draw();
    return { focus: () => root.focus(), dispose };
  } catch (error) {
    dispose();
    throw error;
  }
}
