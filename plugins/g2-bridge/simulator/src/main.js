import "../styles/main.css";
import {
  G2BridgeClient,
  DEFAULT_BRIDGE_URL,
  buildEvenClientModel,
  normalizeEpochSeconds,
} from "./bridge-client.js";
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
  locationLat: document.querySelector("#locationLat"),
  locationLon: document.querySelector("#locationLon"),
  sendLocationButton: document.querySelector("#sendLocationButton"),
  walkButton: document.querySelector("#walkButton"),
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
  // `?omni=0` forces the legacy projects landing for debugging even when the
  // bridge advertises omnipresence (config surface, PRD §3.6).
  forceLegacyView: params.get("omni") === "0",
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
    elements.sendLocationButton,
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
      // Foreground/background transitions arrive as sysEvent frames.
      const eventType =
        event?.textEvent?.eventType ?? event?.listEvent?.eventType ?? event?.sysEvent?.eventType;
      void handleEvenEvent(eventType);
    });
    log("Even bridge ready");
    // Route through the shared chain so the startup render serializes with any
    // renders already in flight and the container is only created once.
    await queueEvenDisplayRender(client.snapshot().glassesText);
    // Even location courier (PRD §3.2): the mini app posts phone fixes to
    // /api/context. Guarded end to end, so 0.0.10 hosts fall back silently.
    await armEvenLocation("startup");
  } catch (err) {
    log(`Even SDK unavailable: ${err.message}`);
  }
}

// OsEventTypeList: 0 = click, 1 = scroll top, 2 = scroll bottom,
// 3 = double click, 4 = foreground enter, 5 = foreground exit.
// Click/double-click semantics are view-aware inside the client: in the feed
// view a click expands the selected push and a double click dismisses it
// (falling through to the OS back convention only when no item is selected);
// everywhere else they stay activate/back.
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
  if (eventType === 4) {
    // FOREGROUND_ENTER: Android may have suspended the WebView and dropped
    // the location subscription -- always re-arm (SDK background caveat).
    void armEvenLocation("foreground-enter");
    return;
  }
  if (eventType === 5) {
    return; // FOREGROUND_EXIT: nothing to do; continuous updates are best-effort.
  }
  log(`Ignoring unrecognized Even hub event type: ${eventType}`);
}

// --- Location courier (EvenHub SDK 0.0.11) ---

const LOCATION_DISTANCE_FILTER_METERS = 50;
const LOCATION_DEDUPE_METERS = 25;

const evenLocation = { unsubscribe: null };

// Arms continuous phone-location updates through the EvenHub SDK 0.0.11 APIs
// (startAppLocationUpdates / onAppLocationChanged). Every SDK touch is
// guarded: on a 0.0.10 host runtime these methods simply do not exist and
// the app falls back silently to manual/browser location controls.
async function armEvenLocation(reason) {
  const bridge = evenDisplay?.bridge;
  if (!bridge) return;
  if (
    typeof bridge.startAppLocationUpdates !== "function" ||
    typeof bridge.onAppLocationChanged !== "function"
  ) {
    if (reason === "startup") {
      log("Even location APIs unavailable (SDK < 0.0.11 host); location courier disabled");
    }
    return;
  }
  try {
    if (!evenLocation.unsubscribe) {
      evenLocation.unsubscribe = bridge.onAppLocationChanged((fix) => {
        void postEvenLocationFix(fix);
      });
    }
    // Medium accuracy with a ~50m distance filter: block-level context
    // without draining the phone (PRD §3.2).
    const started = await bridge.startAppLocationUpdates({
      accuracy: "medium",
      distanceFilter: LOCATION_DISTANCE_FILTER_METERS,
    });
    log(`Even location updates ${started ? "armed" : "not granted"} (${reason})`);
  } catch (err) {
    // PERMISSION_DENIED is expected on QR-sideloaded dev builds; installed
    // Hub builds are the validated path.
    log(`Even location unavailable (${reason}): ${err.message}`);
  }
}

async function postEvenLocationFix(fix) {
  const lat = Number(fix?.latitude ?? fix?.lat);
  const lon = Number(fix?.longitude ?? fix?.lon);
  try {
    // JS SDK fixes report millisecond epochs; the bridge stores seconds.
    // sendLocation normalizes too, but convert here so no layer ever sees a
    // milliseconds value masquerading as a seconds epoch.
    const result = await client.sendLocation(
      { lat, lon, accuracy: fix?.accuracy, ts: normalizeEpochSeconds(fix?.timestamp) },
      { dedupeMeters: LOCATION_DEDUPE_METERS },
    );
    // Coordinates intentionally never hit the debug log (PRD §3.2 privacy).
    log(result ? "Location fix posted to /api/context" : "Location fix skipped (<25m move)");
  } catch (err) {
    log(`Location post failed: ${err.message}`);
  }
}

// --- Browser-mode manual location controls ---

let walkTimer = null;

function readManualCoordinates() {
  return {
    lat: Number.parseFloat(elements.locationLat.value),
    lon: Number.parseFloat(elements.locationLon.value),
  };
}

async function sendManualLocation() {
  saveConfig();
  const { lat, lon } = readManualCoordinates();
  // sendLocation validates client-side and throws on empty/NaN/out-of-range
  // input; runAction surfaces the message without hitting the bridge.
  await client.sendLocation({ lat, lon, ts: Date.now() / 1000 });
  log("Manual location posted to /api/context");
}

// Demo "walk": steps the coordinates ~40m north-east every 2s and posts each
// fix, so location-triggered loops can be exercised without a phone.
function toggleWalkSimulator() {
  if (walkTimer) {
    clearInterval(walkTimer);
    walkTimer = null;
    elements.walkButton.textContent = "Walk";
    log("Walk simulator stopped");
    return;
  }
  const start = readManualCoordinates();
  if (!Number.isFinite(start.lat) || !Number.isFinite(start.lon)) {
    log("Walk simulator needs valid start coordinates");
    return;
  }
  elements.walkButton.textContent = "Stop walk";
  log("Walk simulator started");
  walkTimer = setInterval(() => {
    const lat = Number.parseFloat(elements.locationLat.value) + 0.00035;
    const lon = Number.parseFloat(elements.locationLon.value) + 0.0002;
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    elements.locationLat.value = lat.toFixed(6);
    elements.locationLon.value = lon.toFixed(6);
    client
      .sendLocation({ lat, lon, ts: Date.now() / 1000 })
      .then(() => log("Walk step posted to /api/context"))
      .catch((err) => log(`Walk step failed: ${err.message}`));
  }, 2000);
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

elements.sendLocationButton.addEventListener("click", () => runAction(() => sendManualLocation()));
elements.walkButton.addEventListener("click", () => toggleWalkSimulator());

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
