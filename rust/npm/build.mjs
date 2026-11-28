import { execFileSync } from "node:child_process";
import { copyFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const cwd = fileURLToPath(new URL("../", import.meta.url));
const target = "wasm32-unknown-unknown";
const run = (command, args) => execFileSync(command, args, { cwd, stdio: "inherit" });

run("cargo", ["build", "--locked", "--release", "--lib", "--target", target, "--target-dir", "target"]);
run("wasm-bindgen", [
  `target/${target}/release/hrns_tui.wasm`,
  "--target", "web",
  "--out-dir", "npm/wasm",
]);
copyFileSync(new URL("../../LICENSE", import.meta.url), new URL("LICENSE", import.meta.url));
