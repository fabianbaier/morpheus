import "../styles/main.css";
import { G2BridgeClient, DEFAULT_BRIDGE_URL, buildEvenClientModel } from "./bridge-client.js";
import {
  clearLegacyTokenStorage,
  saveBridgeConfig,
  savePreviewSkin,
  storedBridgeUrl,
  storedSkin,
  storedToken,
} from "./storage.js";

const params = new URLSearchParams(window.location.search);
clearLegacyTokenStorage(window);
const savedBridgeUrl = storedBridgeUrl(window);
const savedToken = storedToken(window, params);
const savedSkin = storedSkin(window);

const elements = {
  bridgeUrl: document.querySelector("#bridgeUrl"),
  bridgeToken: document.querySelector("#bridgeToken"),
  glassesText: document.querySelector("#glassesText"),
  evenClientFrame: document.querySelector("#evenClientFrame"),
  debugLog: document.querySelector("#debugLog"),
  transcriptText: document.querySelector("#transcriptText"),
  connectButton: document.querySelector("#connectButton"),
  refreshButton: document.querySelector("#refreshButton"),
  sendTranscriptButton: document.querySelector("#sendTranscriptButton"),
  upButton: document.querySelector("#upButton"),
  clickButton: document.querySelector("#clickButton"),
  downButton: document.querySelector("#downButton"),
  backButton: document.querySelector("#backButton"),
  morpheusSkinButton: document.querySelector("#morpheusSkinButton"),
  evenSkinButton: document.querySelector("#evenSkinButton"),
};

elements.bridgeUrl.value = params.get("bridge") || savedBridgeUrl || DEFAULT_BRIDGE_URL;
elements.bridgeToken.value = params.has("token") ? params.get("token") || "" : savedToken;

const logs = [];
let previewSkin = normalizeSkin(params.get("skin") || savedSkin);
let evenDisplay = null;
let pendingRender = Promise.resolve();

const client = new G2BridgeClient({
  bridgeUrl: elements.bridgeUrl.value,
  token: elements.bridgeToken.value,
  onChange: render,
  onLog: log,
});

function log(message) {
  const stamp = new Date().toLocaleTimeString();
  logs.push(`[${stamp}] ${message}`);
  while (logs.length > 80) logs.shift();
  elements.debugLog.textContent = logs.join("\n");
  elements.debugLog.scrollTop = elements.debugLog.scrollHeight;
}

function normalizeSkin(value) {
  return value === "even" ? "even" : "morpheus";
}

function setPreviewSkin(skin) {
  previewSkin = normalizeSkin(skin);
  savePreviewSkin(window, previewSkin);
  for (const button of [elements.morpheusSkinButton, elements.evenSkinButton]) {
    const isActive = button.dataset.skin === previewSkin;
    button.setAttribute("aria-pressed", String(isActive));
    button.classList.toggle("is-active", isActive);
  }
  render(client.snapshot());
}

function setBusy(isBusy) {
  for (const button of [
    elements.connectButton,
    elements.refreshButton,
    elements.sendTranscriptButton,
    elements.upButton,
    elements.clickButton,
    elements.downButton,
    elements.backButton,
  ]) {
    button.disabled = isBusy;
  }
}

function saveConfig() {
  // An emptied URL field resets to the default bridge URL everywhere: input,
  // storage, and client stay in sync instead of silently diverging.
  const bridgeUrl = elements.bridgeUrl.value.trim() || DEFAULT_BRIDGE_URL;
  const token = elements.bridgeToken.value.trim();
  elements.bridgeUrl.value = bridgeUrl;
  saveBridgeConfig(window, { bridgeUrl, token });
  client.configure({ bridgeUrl, token });
}

function render(snapshot) {
  const text = snapshot.glassesText || "Morpheus G2 simulator";
  const isEvenSkin = previewSkin === "even";
  elements.glassesText.hidden = isEvenSkin;
  elements.evenClientFrame.hidden = !isEvenSkin;
  if (isEvenSkin) renderEvenClient(snapshot);
  else elements.glassesText.textContent = text;
  queueEvenDisplayRender(text);
}

function queueEvenDisplayRender(text) {
  pendingRender = pendingRender.then(() => renderEvenDisplay(text)).catch((err) => {
    log(`Even display render failed: ${err.message}`);
  });
  return pendingRender;
}

function renderEvenClient(snapshot) {
  const model = buildEvenClientModel(snapshot);
  const frame = elements.evenClientFrame;
  frame.replaceChildren();

  const viewport = document.createElement("div");
  viewport.className = "even-client-viewport";

  const topRow = document.createElement("div");
  topRow.className = "even-client-top-row";
  const primaryAction = document.createElement("div");
  primaryAction.className = "even-client-primary-action";
  primaryAction.textContent = model.mode === "session" ? model.title : model.addSessionLabel;
  topRow.append(primaryAction);
  viewport.append(topRow);

  const content = document.createElement("div");
  content.className = "even-client-content";
  if (model.error) {
    content.append(lineElement(`/ error ${model.error}`, "is-error"));
  } else if (model.mode === "session") {
    const messages = model.messages.length ? model.messages : ["/ waiting for stream"];
    for (const message of messages) content.append(lineElement(message));
  } else {
    const rows = model.rows.length
      ? model.rows
      : [{ label: model.mode === "projects" ? "/ no projects" : "/ no sessions", selected: false }];
    for (const row of rows) {
      content.append(lineElement(row.label, row.selected ? "is-selected" : ""));
    }
  }
  viewport.append(content);
  frame.append(viewport);
}

function lineElement(text, extraClass = "") {
  const line = document.createElement("div");
  line.className = ["even-client-line", extraClass].filter(Boolean).join(" ");
  line.textContent = text;
  return line;
}

async function renderEvenDisplay(text) {
  if (!evenDisplay?.bridge) return;
  const clipped = text.slice(0, 1800);
  if (!evenDisplay.created) {
    const textObject = new evenDisplay.sdk.TextContainerProperty({
      xPosition: 0,
      yPosition: 0,
      width: 576,
      height: 288,
      borderWidth: 0,
      borderColor: 5,
      paddingLength: 4,
      containerID: 1,
      containerName: "main",
      content: clipped.slice(0, 950),
      isEventCapture: 1,
    });
    const result = await evenDisplay.bridge.createStartUpPageContainer(
      new evenDisplay.sdk.CreateStartUpPageContainer({
        containerTotalNum: 1,
        textObject: [textObject],
      }),
    );
    evenDisplay.created = true;
    log(`Even startup page result: ${result}`);
    return;
  }
  await evenDisplay.bridge.textContainerUpgrade(1, "main", clipped, 0, clipped.length);
}

async function initEvenDisplay() {
  const evenMode = params.get("even");
  const hasFlutterHandler = Boolean(window.flutter_inappwebview?.callHandler);
  if (evenMode !== "1" && (evenMode === "0" || !hasFlutterHandler)) {
    log("Even bridge not detected; browser preview mode is active");
    return;
  }
  try {
    const sdk = await import("@evenrealities/even_hub_sdk");
    const bridge = await Promise.race([
      sdk.waitForEvenAppBridge(),
      new Promise((resolve) => setTimeout(() => resolve(null), 1800)),
    ]);
    if (!bridge) {
      log("Even bridge not detected; browser preview mode is active");
      return;
    }
    evenDisplay = { sdk, bridge, created: false };
    bridge.onEvenHubEvent((event) => {
      const eventType = event?.textEvent?.eventType ?? event?.listEvent?.eventType;
      void handleEvenEvent(eventType);
    });
    log("Even bridge ready");
    // Route through the shared chain so the startup render serializes with any
    // renders already in flight and the container is only created once.
    await queueEvenDisplayRender(client.snapshot().glassesText);
  } catch (err) {
    log(`Even SDK unavailable: ${err.message}`);
  }
}

// OsEventTypeList: 0 = click, 1 = scroll top, 2 = scroll bottom, 3 = double click.
async function handleEvenEvent(eventType) {
  if (eventType === 0) {
    await runAction(() => client.activateSelected());
    return;
  }
  if (eventType === 1) {
    client.move(-1);
    return;
  }
  if (eventType === 2) {
    client.move(1);
    return;
  }
  if (eventType === 3) {
    await runAction(() => client.navigateBack());
    return;
  }
  log(`Ignoring unrecognized Even hub event type: ${eventType}`);
}

let actionInFlight = false;

async function runAction(action) {
  // Buttons are disabled while busy, but Enter/Escape and Even hub gestures
  // land here too; dropping them keeps async actions from overlapping.
  if (actionInFlight) return;
  actionInFlight = true;
  setBusy(true);
  try {
    await action();
  } catch (err) {
    client.state = { ...client.state, error: err.message };
    client.emit();
    log(err.message);
  } finally {
    actionInFlight = false;
    setBusy(false);
  }
}

elements.connectButton.addEventListener("click", () =>
  runAction(async () => {
    saveConfig();
    await client.connect();
  }),
);

elements.refreshButton.addEventListener("click", () =>
  runAction(async () => {
    saveConfig();
    if (client.state.mode === "projects") await client.refreshProjects();
    else if (client.state.mode === "sessions") await client.refreshSessions();
    else await client.refreshMessages();
  }),
);

elements.sendTranscriptButton.addEventListener("click", () =>
  runAction(async () => {
    saveConfig();
    await client.submitTranscriptViaSessionPolling(elements.transcriptText.value);
  }),
);

elements.upButton.addEventListener("click", () => client.move(-1));
elements.downButton.addEventListener("click", () => client.move(1));
elements.clickButton.addEventListener("click", () => runAction(() => client.activateSelected()));
elements.backButton.addEventListener("click", () => runAction(() => client.navigateBack()));
elements.morpheusSkinButton.addEventListener("click", () => setPreviewSkin("morpheus"));
elements.evenSkinButton.addEventListener("click", () => setPreviewSkin("even"));

window.addEventListener("keydown", (event) => {
  if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
  if (event.key === "ArrowUp") {
    event.preventDefault();
    client.move(-1);
  } else if (event.key === "ArrowDown") {
    event.preventDefault();
    client.move(1);
  } else if (event.key === "Enter") {
    event.preventDefault();
    void runAction(() => client.activateSelected());
  } else if (event.key === "Escape") {
    event.preventDefault();
    void runAction(() => client.navigateBack());
  }
});

setPreviewSkin(previewSkin);
void initEvenDisplay();
