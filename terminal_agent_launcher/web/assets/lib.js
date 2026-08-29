export function shellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

export const PERMISSION_PRESETS = {
  claude: [
    {
      id: "plan",
      name: "Plan",
      description: "Read the project and make a plan without editing files.",
      arguments: ["--permission-mode", "plan"],
    },
    {
      id: "default",
      name: "Default",
      description: "Ask before edits, commands, and other protected actions.",
      arguments: ["--permission-mode", "default"],
    },
    {
      id: "accept-edits",
      name: "Accept Edits",
      description: "Apply file edits automatically; keep other permission prompts.",
      arguments: ["--permission-mode", "acceptEdits"],
    },
    {
      id: "auto",
      name: "Auto",
      description: "Use Claude safety checks instead of routine prompts, if available.",
      arguments: ["--permission-mode", "auto"],
    },
    {
      id: "bypass",
      name: "Full Bypass",
      description: "Skip permission prompts and safety checks for this session.",
      arguments: ["--dangerously-skip-permissions"],
      danger: true,
    },
  ],
  codex: [
    {
      id: "read-only",
      name: "Read Only",
      description: "Inspect the project without writing files.",
      arguments: ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
    },
    {
      id: "workspace",
      name: "Workspace Write",
      description: "Write inside the project and ask before elevated actions.",
      arguments: ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"],
    },
    {
      id: "autonomous",
      name: "Autonomous Workspace",
      description: "Work inside the project without asking for approval.",
      arguments: ["--sandbox", "workspace-write", "--ask-for-approval", "never"],
    },
    {
      id: "bypass",
      name: "Full Bypass",
      description: "Remove approval prompts and sandbox protections.",
      arguments: ["--dangerously-bypass-approvals-and-sandbox"],
      danger: true,
    },
  ],
};

export const DEFAULT_PERMISSIONS = {
  claude: "default",
  codex: "workspace",
};

export function permissionPresetsFor(agent) {
  const presets = PERMISSION_PRESETS[agent];
  if (!presets) throw new TypeError("Agent must be claude or codex.");
  return presets;
}

export function buildCommandPreview(path, agent, permission) {
  if (!path) throw new TypeError("A folder path is required.");
  const preset = permissionPresetsFor(agent).find((item) => item.id === permission);
  if (!preset) throw new TypeError("A supported permission preset is required.");
  const argumentsText = preset.arguments.join(" ");
  if (agent === "claude") {
    return `cd ${shellQuote(path)} && claude ${argumentsText}`;
  }
  return `codex --cd ${shellQuote(path)} ${argumentsText}`;
}

export function readMigratedStorage(
  storage,
  canonicalKey,
  legacyKey,
  fallback,
  validate = () => true,
) {
  const read = (key) => {
    if (!key) return { found: false };
    try {
      const raw = storage.getItem(key);
      if (raw === null) return { found: false };
      return { found: true, value: JSON.parse(raw) };
    } catch {
      return { found: true };
    }
  };

  const canonical = read(canonicalKey);
  if (canonical.found && validate(canonical.value)) return canonical.value;

  const legacy = read(legacyKey);
  if (!legacy.found || !validate(legacy.value)) return fallback;
  try {
    storage.setItem(canonicalKey, JSON.stringify(legacy.value));
  } catch {
    // Storage can be unavailable in private or locked-down browser contexts.
  }
  return legacy.value;
}

export function filterProjects(projects, options = {}) {
  const {
    query = "",
    view = "all",
    favorites = new Set(),
    recents = [],
  } = options;
  const tokens = query
    .trim()
    .toLocaleLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  const recentTimes = new Map(recents.map((recent) => [recent.path, recent.launchedAt]));

  const filtered = projects.filter((project) => {
    if (view === "favorites" && !favorites.has(project.path)) return false;
    if (view === "recents" && !recentTimes.has(project.path)) return false;
    const haystack = `${project.name} ${project.path}`.toLocaleLowerCase();
    return tokens.every((token) => haystack.includes(token));
  });

  if (view === "recents") {
    return filtered.sort((left, right) => recentTimes.get(right.path) - recentTimes.get(left.path));
  }
  return filtered.sort((left, right) =>
    left.name.localeCompare(right.name, undefined, { numeric: true, sensitivity: "base" }),
  );
}

export function formatModified(timestampSeconds, nowMilliseconds = Date.now()) {
  const date = new Date(timestampSeconds * 1000);
  const now = new Date(nowMilliseconds);
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfDate = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const dayDifference = Math.round((startOfToday - startOfDate) / 86_400_000);

  if (dayDifference === 0) {
    return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }
  if (dayDifference === 1) return "Yesterday";
  if (date.getFullYear() === now.getFullYear()) {
    return date.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  return date.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}
