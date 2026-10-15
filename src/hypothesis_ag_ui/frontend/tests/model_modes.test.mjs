import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");

test("plan, continue, and revise submit both selected model modes", () => {
  for (const [action, selection] of [
    ["plan", "planModelModes"],
    ["continue", "continueModelModes"],
    ["revise", "reviseModelModes"],
  ]) {
    assert.match(
      source,
      new RegExp(`action:\\s*"${action}"[^\\n]+model_modes:\\s*${selection}`),
      `${action} must send its selected planning and execution modes`,
    );
    assert.match(source, new RegExp(`useState<ModelModes>\\(defaultModelModes\\)`));
  }
});

test("phase renders immutable planning and execution mode badges", () => {
  assert.match(source, /phaseModelModes\(phase/);
  assert.match(source, /ModelModeBadges modelModes=\{selectedModelModes\}/);
  assert.match(source, /Planning:\s*\{modelModeLabel\(modelModes\.planning\)\}/i);
  assert.match(source, /Execution:\s*\{modelModeLabel\(modelModes\.execution\)\}/i);
});

test("approval wording identifies the immutable execution mode", () => {
  assert.match(
    source,
    /Approve & execute with \{modelModeLabel\(selectedModelModes\.execution\)\}/i,
  );
  assert.doesNotMatch(
    source,
    /action:\s*"approve"[\s\S]{0,500}model_modes:/,
    "approval must not submit replacement model modes",
  );
});
