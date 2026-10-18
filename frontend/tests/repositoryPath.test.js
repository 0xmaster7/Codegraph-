import assert from "node:assert/strict";
import test from "node:test";
import { resolveRepositoryPath } from "../src/ui/repositoryPath.js";

test("change analysis uses the stable repository path from snapshot metadata", () => {
  assert.equal(
    resolveRepositoryPath(
      "/checkout/click",
      "/checkout/click/.codegraph-cache/m21-commits/8b19813f2bfc-temporary",
    ),
    "/checkout/click",
  );
});

test("change analysis falls back to the analyzed path when metadata has no repository root", () => {
  assert.equal(resolveRepositoryPath(undefined, "/checkout/click"), "/checkout/click");
});
