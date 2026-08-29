import test from "node:test";
import assert from "node:assert/strict";

import {
  buildCommandPreview,
  DEFAULT_PERMISSIONS,
  filterProjects,
  formatModified,
  permissionPresetsFor,
  readMigratedStorage,
  shellQuote,
} from "../terminal_agent_launcher/web/assets/lib.js";

const projects = [
  { name: "Zebra", path: "/Users/example/Projects/Zebra" },
  { name: "terminal-agent-launcher", path: "/Users/example/Projects/terminal-agent-launcher" },
  { name: "Alpha 2", path: "/Users/example/Work/Alpha 2" },
];

test("shellQuote safely quotes apostrophes", () => {
  assert.equal(shellQuote("/Users/O'Brien/Test"), "'/Users/O'\\''Brien/Test'");
});

test("Codex preview includes its workspace permission flags", () => {
  assert.equal(
    buildCommandPreview("/Users/example/Projects/My Project", "codex", "workspace"),
    "codex --cd '/Users/example/Projects/My Project' --sandbox workspace-write --ask-for-approval on-request",
  );
});

test("Claude preview changes directory and includes its permission mode", () => {
  assert.equal(
    buildCommandPreview("/Users/example/Projects/Alpha", "claude", "plan"),
    "cd '/Users/example/Projects/Alpha' && claude --permission-mode plan",
  );
});

test("permission presets expose safe defaults and flagged bypass modes", () => {
  assert.equal(DEFAULT_PERMISSIONS.claude, "default");
  assert.equal(DEFAULT_PERMISSIONS.codex, "workspace");
  assert.equal(permissionPresetsFor("claude").length, 5);
  assert.equal(permissionPresetsFor("codex").find((preset) => preset.id === "bypass").danger, true);
});

test("project filtering matches tokens in names and paths", () => {
  const result = filterProjects(projects, { query: "work alpha" });
  assert.deepEqual(result.map((project) => project.name), ["Alpha 2"]);
});

test("favorite and recent views use their stored paths", () => {
  const favorites = filterProjects(projects, {
    view: "favorites",
    favorites: new Set([projects[1].path]),
  });
  assert.deepEqual(favorites.map((project) => project.name), ["terminal-agent-launcher"]);

  const recents = filterProjects(projects, {
    view: "recents",
    recents: [
      { path: projects[0].path, launchedAt: 20 },
      { path: projects[2].path, launchedAt: 40 },
    ],
  });
  assert.deepEqual(recents.map((project) => project.name), ["Alpha 2", "Zebra"]);
});

test("formatModified renders yesterday deterministically", () => {
  const now = new Date(2026, 7, 28, 12, 0).getTime();
  const yesterday = new Date(2026, 7, 27, 9, 0).getTime() / 1000;
  assert.equal(formatModified(yesterday, now), "Yesterday");
});

test("legacy browser storage migrates without deleting the old value", () => {
  const values = new Map([
    ["agent-launchpad:favorites", JSON.stringify(["/Users/example/Projects/Alpha"])],
  ]);
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };

  const migrated = readMigratedStorage(
    storage,
    "terminal-agent-launcher:favorites",
    "agent-launchpad:favorites",
    [],
    (value) => Array.isArray(value),
  );

  assert.deepEqual(migrated, ["/Users/example/Projects/Alpha"]);
  assert.equal(values.has("agent-launchpad:favorites"), true);
  assert.deepEqual(
    JSON.parse(values.get("terminal-agent-launcher:favorites")),
    migrated,
  );
});

test("valid canonical browser storage wins over legacy data", () => {
  const values = new Map([
    ["terminal-agent-launcher:recents", JSON.stringify(["canonical"])],
    ["agent-launchpad:recents", JSON.stringify(["legacy"])],
  ]);
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };

  assert.deepEqual(
    readMigratedStorage(
      storage,
      "terminal-agent-launcher:recents",
      "agent-launchpad:recents",
      [],
      Array.isArray,
    ),
    ["canonical"],
  );
});
