import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const shellCss = await readFile(new URL("../src/ui/productShell.css", import.meta.url), "utf8");

test("product shell and page content use the available width", () => {
  assert.match(shellCss, /\.product-shell\s*\{[^}]*width:\s*100%/s);
  assert.match(shellCss, /\.product-content\s*\{[^}]*align-items:\s*stretch/s);
  assert.match(shellCss, /\.repository-start\s*\{[^}]*width:\s*100%;[^}]*max-width:\s*1440px/s);
});
