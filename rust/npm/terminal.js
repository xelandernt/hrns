let nextTerminalId = 0;

export function attachTerminal(WebTerminal, container, options = {}) {
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

  let terminal;
  let observer;
  let frame;
  let resize = false;
  let disposed = false;
  let escapePressed = false;

  function allowTabOut(event) {
    if (event.key === "Shift") return;
    const leaving = escapePressed && event.key === "Tab";
    escapePressed = event.key === "Escape" && !event.isComposing;
    if (leaving) event.stopImmediatePropagation();
  }

  function resetEscape() {
    escapePressed = false;
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    signal?.removeEventListener("abort", dispose);
    root.removeEventListener("keydown", allowTabOut, true);
    root.removeEventListener("blur", resetEscape);
    view.cancelAnimationFrame(frame);
    observer?.disconnect();
    try {
      terminal?.free();
    } finally {
      root.remove();
    }
  }

  function draw() {
    try {
      if (resize) {
        resize = false;
        terminal.resize();
      }
      terminal.draw();
      frame = view.requestAnimationFrame(draw);
    } catch (error) {
      dispose();
      throw error;
    }
  }

  try {
    root.addEventListener("keydown", allowTabOut, true);
    root.addEventListener("blur", resetEscape);
    terminal = new WebTerminal(root, input, endpoint, cwd, session, theme);
    observer = new view.ResizeObserver(() => { resize = true; });
    observer.observe(root);
    signal?.addEventListener("abort", dispose, { once: true });
    draw();
    return { focus: () => root.focus(), dispose };
  } catch (error) {
    dispose();
    throw error;
  }
}
