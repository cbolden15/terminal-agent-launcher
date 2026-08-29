import {
  buildCommandPreview,
  DEFAULT_PERMISSIONS,
  filterProjects,
  formatModified,
  permissionPresetsFor,
  readMigratedStorage,
} from "./lib.js";

const STORAGE = {
  favorites: "terminal-agent-launcher:favorites",
  launchPreferences: "terminal-agent-launcher:launch-preferences",
  recents: "terminal-agent-launcher:recents",
};

const LEGACY_STORAGE = {
  favorites: "agent-launchpad:favorites",
  launchPreferences: "agent-launchpad:launch-preferences",
  recents: "agent-launchpad:recents",
};

const MUTATION_HEADERS = {
  "Content-Type": "application/json",
  "X-Terminal-Agent-Launcher": "1",
};

const state = {
  projects: [],
  locations: [],
  status: {},
  view: "all",
  query: "",
  selectedPath: null,
  favorites: new Set(
    readStorage(
      STORAGE.favorites,
      LEGACY_STORAGE.favorites,
      [],
      isStringArray,
    ),
  ),
  launchBusy: false,
  launchPreferences: readStorage(
    STORAGE.launchPreferences,
    LEGACY_STORAGE.launchPreferences,
    {
      claude: DEFAULT_PERMISSIONS.claude,
      codex: DEFAULT_PERMISSIONS.codex,
      destination: "tab",
    },
    isLaunchPreferences,
  ),
  launchTarget: null,
  recents: readStorage(
    STORAGE.recents,
    LEGACY_STORAGE.recents,
    [],
    isRecents,
  ),
  loading: true,
};

const elements = {
  addButtons: [
    document.querySelector("#add-location"),
    document.querySelector("#add-location-sidebar"),
    document.querySelector("#empty-add-location"),
  ],
  closeDialog: document.querySelector("#close-location-dialog"),
  cancelDialog: document.querySelector("#cancel-location"),
  contentTitle: document.querySelector("#view-title"),
  dialog: document.querySelector("#location-dialog"),
  empty: document.querySelector("#empty-state"),
  form: document.querySelector("#location-form"),
  formError: document.querySelector("#location-error"),
  loading: document.querySelector("#loading-state"),
  locations: document.querySelector("#locations-list"),
  launchAgentBadge: document.querySelector("#launch-agent-badge"),
  launchButton: document.querySelector("#confirm-launch"),
  launchCancel: document.querySelector("#cancel-launch"),
  launchClose: document.querySelector("#close-launch-dialog"),
  launchCommand: document.querySelector("#launch-command"),
  launchConfirmBypass: document.querySelector("#confirm-bypass"),
  launchDialog: document.querySelector("#launch-dialog"),
  launchError: document.querySelector("#launch-error"),
  launchForm: document.querySelector("#launch-form"),
  launchPermissions: document.querySelector("#launch-permissions"),
  launchProjectName: document.querySelector("#launch-project-name"),
  launchProjectPath: document.querySelector("#launch-project-path"),
  launchTitle: document.querySelector("#launch-title"),
  launchWarning: document.querySelector("#bypass-warning"),
  pathInput: document.querySelector("#location-path"),
  projectList: document.querySelector("#project-list"),
  refresh: document.querySelector("#refresh-projects"),
  search: document.querySelector("#project-search"),
  summary: document.querySelector("#project-summary"),
  toast: document.querySelector("#toast"),
};

function isStringArray(value) {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isRecents(value) {
  return Array.isArray(value) && value.every((item) => (
    item
    && typeof item.path === "string"
    && Number.isFinite(item.launchedAt)
    && ["claude", "codex"].includes(item.lastAgent)
  ));
}

function isLaunchPreferences(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const validClaude = value.claude === undefined
    || permissionPresetsFor("claude").some((preset) => preset.id === value.claude);
  const validCodex = value.codex === undefined
    || permissionPresetsFor("codex").some((preset) => preset.id === value.codex);
  const validDestination = value.destination === undefined
    || ["tab", "window"].includes(value.destination);
  return validClaude && validCodex && validDestination;
}

function readStorage(key, legacyKey, fallback, validate) {
  return readMigratedStorage(
    localStorage,
    key,
    legacyKey,
    fallback,
    validate,
  );
}

function writeStorage(key, value) {
  localStorage.setItem(key, JSON.stringify(value));
}

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

async function loadState({ quiet = false } = {}) {
  if (!quiet) {
    state.loading = true;
    render();
  }
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load projects.");
    state.projects = payload.projects;
    state.locations = payload.locations;
    state.status = payload.status;
    state.loading = false;
    ensureSelection();
    render();
    return true;
  } catch (error) {
    state.loading = false;
    state.projects = [];
    render();
    showToast(error.message, true);
    return false;
  }
}

function visibleProjects() {
  return filterProjects(state.projects, {
    query: state.query,
    view: state.view,
    favorites: state.favorites,
    recents: state.recents,
  });
}

function ensureSelection(projects = visibleProjects()) {
  if (!projects.some((project) => project.path === state.selectedPath)) {
    state.selectedPath = projects[0]?.path ?? null;
  }
}

function render() {
  renderNavigation();
  renderLocations();
  renderStatus();

  elements.loading.hidden = !state.loading;
  if (state.loading) {
    elements.projectList.replaceChildren();
    elements.empty.hidden = true;
    elements.summary.textContent = "Loading folders";
    return;
  }

  const projects = visibleProjects();
  ensureSelection(projects);
  renderProjects(projects);
  elements.empty.hidden = projects.length !== 0;
  elements.summary.textContent = projectSummary(projects.length);
}

function renderNavigation() {
  const recentPaths = new Set(state.recents.map((recent) => recent.path));
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === state.view);
  });
  document.querySelector('[data-count="all"]').textContent = state.projects.length;
  document.querySelector('[data-count="favorites"]').textContent = state.projects.filter((project) => state.favorites.has(project.path)).length;
  document.querySelector('[data-count="recents"]').textContent = state.projects.filter((project) => recentPaths.has(project.path)).length;
  elements.contentTitle.textContent = {
    all: "All Projects",
    favorites: "Favorites",
    recents: "Recents",
  }[state.view];
}

function renderLocations() {
  const fragment = document.createDocumentFragment();
  for (const location of state.locations) {
    const item = createElement("div", `location-item${location.exists ? "" : " is-missing"}`);
    item.title = location.path;

    const icon = createElement("span", "location-icon");
    icon.setAttribute("aria-hidden", "true");
    const copy = createElement("span", "location-copy");
    copy.append(
      createElement("strong", null, location.name),
      createElement("small", null, location.kind === "root" ? `${location.project_count} folders` : "Single folder"),
    );
    const remove = createElement("button", "location-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${location.name}`);
    remove.addEventListener("click", () => removeLocation(location));
    item.append(icon, copy, remove);
    fragment.append(item);
  }
  elements.locations.replaceChildren(fragment);
}

function renderStatus() {
  for (const tool of ["iterm", "claude", "codex"]) {
    const item = document.querySelector(`[data-status="${tool}"]`);
    item.classList.toggle("is-ready", Boolean(state.status[tool]));
    item.title = state.status[tool] ? "Available" : "Not detected";
  }
}

function renderProjects(projects) {
  const fragment = document.createDocumentFragment();
  projects.forEach((project) => fragment.append(projectRow(project)));
  elements.projectList.replaceChildren(fragment);
}

function projectRow(project) {
  const selected = project.path === state.selectedPath;
  const row = createElement("div", `project-row${selected ? " is-selected" : ""}`);
  row.dataset.path = project.path;
  row.setAttribute("role", "option");
  row.setAttribute("aria-selected", String(selected));
  row.tabIndex = selected ? 0 : -1;

  const favorite = createElement("button", "favorite-button", state.favorites.has(project.path) ? "★" : "☆");
  favorite.type = "button";
  favorite.setAttribute("aria-label", state.favorites.has(project.path) ? `Remove ${project.name} from favorites` : `Add ${project.name} to favorites`);
  favorite.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleFavorite(project.path);
  });

  const nameCell = createElement("div", "name-cell");
  const folder = createElement("span", "folder-icon");
  folder.setAttribute("aria-hidden", "true");
  const nameCopy = createElement("span", "name-copy");
  nameCopy.append(createElement("strong", null, project.name));
  if (project.is_git) nameCopy.append(createElement("small", null, "Git repository"));
  nameCell.append(folder, nameCopy);

  const path = createElement("span", "path-cell", project.path);
  path.title = project.path;
  const modified = createElement("span", "modified-cell", formatModified(project.modified_at));
  const actions = createElement("div", "agent-actions");
  for (const agent of ["claude", "codex"]) {
    const button = createElement("button", `agent-button ${agent}`, agent === "claude" ? "Claude" : "Codex");
    button.type = "button";
    button.disabled = state.status[agent] === false || state.status.iterm === false;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openLaunchDialog(project, agent);
    });
    actions.append(button);
  }

  row.append(favorite, nameCell, path, modified, actions);
  row.addEventListener("pointerdown", (event) => {
    if (!event.target.closest("button")) selectProject(project.path, false);
  });
  row.addEventListener("click", (event) => {
    if (!event.target.closest("button")) selectProject(project.path, true);
  });
  return row;
}

function selectProject(path, focus) {
  state.selectedPath = path;
  render();
  if (focus) {
    document.querySelector(`.project-row[data-path="${CSS.escape(path)}"]`)?.focus({ preventScroll: true });
  }
}

function toggleFavorite(path) {
  if (state.favorites.has(path)) state.favorites.delete(path);
  else state.favorites.add(path);
  writeStorage(STORAGE.favorites, [...state.favorites]);
  render();
}

function openLaunchDialog(project, agent) {
  if (state.status.iterm === false || state.status[agent] === false) {
    showToast(`${agent === "claude" ? "Claude" : "Codex"} or iTerm2 was not detected.`, true);
    return;
  }
  const agentName = agent === "claude" ? "Claude" : "Codex";
  const presets = permissionPresetsFor(agent);
  const storedPermission = state.launchPreferences?.[agent];
  const permission = presets.some((preset) => preset.id === storedPermission && !preset.danger)
    ? storedPermission
    : DEFAULT_PERMISSIONS[agent];
  const destination = ["tab", "window"].includes(state.launchPreferences?.destination)
    ? state.launchPreferences.destination
    : "tab";

  state.launchTarget = { project, agent };
  state.launchBusy = false;
  elements.launchDialog.dataset.agent = agent;
  elements.launchTitle.textContent = `Open with ${agentName}`;
  elements.launchProjectName.textContent = project.name;
  elements.launchProjectPath.textContent = project.path;
  elements.launchAgentBadge.className = `agent-badge ${agent}`;
  elements.launchAgentBadge.textContent = agentName;
  elements.launchError.hidden = true;
  elements.launchConfirmBypass.checked = false;
  renderPermissionOptions(agent, permission);
  elements.launchForm.querySelector(`input[name="destination"][value="${destination}"]`).checked = true;
  updateLaunchDialog();
  elements.launchDialog.showModal();
  requestAnimationFrame(() => {
    elements.launchForm.querySelector("input[name='permission']:checked")?.focus();
  });
}

function renderPermissionOptions(agent, selectedPermission) {
  const fragment = document.createDocumentFragment();
  for (const preset of permissionPresetsFor(agent)) {
    const label = createElement("label", `permission-option${preset.danger ? " is-danger" : ""}`);
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "permission";
    input.value = preset.id;
    input.checked = preset.id === selectedPermission;
    const copy = createElement("span", "permission-copy");
    copy.append(
      createElement("strong", null, preset.name),
      createElement("small", null, preset.description),
    );
    label.append(input, copy);
    fragment.append(label);
  }
  elements.launchPermissions.replaceChildren(fragment);
}

function selectedLaunchOptions() {
  const formData = new FormData(elements.launchForm);
  return {
    permission: formData.get("permission"),
    destination: formData.get("destination"),
  };
}

function updateLaunchDialog() {
  if (!state.launchTarget) return;
  const { project, agent } = state.launchTarget;
  const { permission } = selectedLaunchOptions();
  const isBypass = permission === "bypass";
  const agentName = agent === "claude" ? "Claude" : "Codex";
  elements.launchWarning.hidden = !isBypass;
  elements.launchConfirmBypass.required = isBypass;
  elements.launchButton.classList.toggle("is-danger", isBypass);
  elements.launchButton.disabled = state.launchBusy || (isBypass && !elements.launchConfirmBypass.checked);
  elements.launchButton.textContent = state.launchBusy ? "Launching…" : `Launch ${agentName}`;
  elements.launchCommand.textContent = buildCommandPreview(project.path, agent, permission);
}

function changeLaunchOptions() {
  elements.launchError.hidden = true;
  updateLaunchDialog();
}

function closeLaunchDialog() {
  if (!state.launchBusy) elements.launchDialog.close();
}

async function submitLaunch(event) {
  event.preventDefault();
  if (!state.launchTarget || state.launchBusy) return;
  const { project, agent } = state.launchTarget;
  const { permission, destination } = selectedLaunchOptions();
  const confirmedBypass = permission === "bypass" && elements.launchConfirmBypass.checked;
  if (permission === "bypass" && !confirmedBypass) {
    updateLaunchDialog();
    return;
  }

  elements.launchError.hidden = true;
  state.launchBusy = true;
  updateLaunchDialog();
  try {
    const response = await fetch("/api/launch", {
      method: "POST",
      headers: MUTATION_HEADERS,
      body: JSON.stringify({
        project_id: project.id,
        agent,
        permission,
        destination,
        confirmed_bypass: confirmedBypass,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not launch that session.");

    const entry = { path: project.path, launchedAt: Date.now(), lastAgent: agent };
    state.recents = [entry, ...state.recents.filter((recent) => recent.path !== project.path)].slice(0, 20);
    state.launchPreferences = {
      ...state.launchPreferences,
      destination,
      ...(permission === "bypass" ? {} : { [agent]: permission }),
    };
    writeStorage(STORAGE.recents, state.recents);
    writeStorage(STORAGE.launchPreferences, state.launchPreferences);
    render();
    elements.launchDialog.close();
    showToast(`Opened ${project.name} in a new ${destination}`);
  } catch (error) {
    elements.launchError.textContent = error.message;
    elements.launchError.hidden = false;
  } finally {
    state.launchBusy = false;
    if (elements.launchDialog.open) updateLaunchDialog();
  }
}

function projectSummary(count) {
  if (state.query) return `${count} ${count === 1 ? "match" : "matches"}`;
  return `${count} ${count === 1 ? "folder" : "folders"}`;
}

function openLocationDialog() {
  elements.form.reset();
  elements.formError.hidden = true;
  elements.dialog.showModal();
  requestAnimationFrame(() => elements.pathInput.focus());
}

function closeLocationDialog() {
  elements.dialog.close();
}

async function submitLocation(event) {
  event.preventDefault();
  const formData = new FormData(elements.form);
  elements.formError.hidden = true;
  try {
    const response = await fetch("/api/locations", {
      method: "POST",
      headers: MUTATION_HEADERS,
      body: JSON.stringify({ path: formData.get("path"), kind: formData.get("kind") }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not add that folder.");
    applyPayload(payload);
    closeLocationDialog();
    showToast("Folder added");
  } catch (error) {
    elements.formError.textContent = error.message;
    elements.formError.hidden = false;
  }
}

async function removeLocation(location) {
  try {
    const response = await fetch("/api/locations", {
      method: "DELETE",
      headers: MUTATION_HEADERS,
      body: JSON.stringify({ path: location.path, kind: location.kind }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not remove that location.");
    applyPayload(payload);
    showToast("Location removed");
  } catch (error) {
    showToast(error.message, true);
  }
}

function applyPayload(payload) {
  state.projects = payload.projects;
  state.locations = payload.locations;
  state.status = payload.status;
  state.loading = false;
  ensureSelection();
  render();
}

let toastTimer;
function showToast(message, isError = false) {
  clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.toggle("is-error", isError);
  elements.toast.hidden = false;
  toastTimer = setTimeout(() => {
    elements.toast.hidden = true;
  }, 2600);
}

function moveSelection(direction) {
  const projects = visibleProjects();
  if (!projects.length) return;
  const currentIndex = projects.findIndex((project) => project.path === state.selectedPath);
  const nextIndex = Math.min(Math.max(currentIndex + direction, 0), projects.length - 1);
  state.selectedPath = projects[nextIndex].path;
  render();
  const row = document.querySelector(`.project-row[data-path="${CSS.escape(state.selectedPath)}"]`);
  row?.focus({ preventScroll: false });
  row?.scrollIntoView({ block: "nearest" });
}

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => {
    state.view = button.dataset.view;
    state.query = "";
    elements.search.value = "";
    ensureSelection();
    render();
  });
});

elements.search.addEventListener("input", () => {
  state.query = elements.search.value;
  ensureSelection();
  render();
});
elements.refresh.addEventListener("click", async () => {
  if (await loadState({ quiet: true })) showToast("Projects refreshed");
});
elements.addButtons.forEach((button) => button.addEventListener("click", openLocationDialog));
elements.closeDialog.addEventListener("click", closeLocationDialog);
elements.cancelDialog.addEventListener("click", closeLocationDialog);
elements.form.addEventListener("submit", submitLocation);
elements.dialog.addEventListener("click", (event) => {
  if (event.target === elements.dialog) closeLocationDialog();
});
elements.launchClose.addEventListener("click", closeLaunchDialog);
elements.launchCancel.addEventListener("click", closeLaunchDialog);
elements.launchForm.addEventListener("submit", submitLaunch);
elements.launchPermissions.addEventListener("change", changeLaunchOptions);
elements.launchForm.querySelectorAll('input[name="destination"]').forEach((input) => {
  input.addEventListener("change", changeLaunchOptions);
});
elements.launchConfirmBypass.addEventListener("change", changeLaunchOptions);
elements.launchDialog.addEventListener("click", (event) => {
  if (event.target === elements.launchDialog) closeLaunchDialog();
});
elements.launchDialog.addEventListener("cancel", (event) => {
  if (state.launchBusy) event.preventDefault();
});
elements.launchDialog.addEventListener("close", () => {
  state.launchTarget = null;
  state.launchBusy = false;
});

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    elements.search.focus();
    elements.search.select();
    return;
  }
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "Escape" && document.activeElement === elements.search && state.query) {
    state.query = "";
    elements.search.value = "";
    render();
    return;
  }
  if (elements.dialog.open || elements.launchDialog.open || ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    moveSelection(event.key === "ArrowDown" ? 1 : -1);
    return;
  }
  const project = state.projects.find((item) => item.path === state.selectedPath);
  if (!project) return;
  if (event.key.toLowerCase() === "c") openLaunchDialog(project, "claude");
  if (event.key.toLowerCase() === "x") openLaunchDialog(project, "codex");
});

loadState();
