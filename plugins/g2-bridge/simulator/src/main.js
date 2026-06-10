import "../styles/main.css";
import { G2BridgeClient, DEFAULT_BRIDGE_URL } from "./bridge-client.js";

const params = new URLSearchParams(window.location.search);
const savedBridgeUrl = window.localStorage.getItem("morpheus:g2:bridgeUrl") || "";
const savedToken = window.localStorage.getItem("morpheus:g2:token") || "";

const elements = {
  bridgeUrl: document.querySelector("#bridgeUrl"),
  bridgeToken: document.querySelector("#bridgeToken"),
  glassesText: document.querySelector("#glassesText"),
  debugLog: document.querySelector("#debugLog"),
  transcriptText: document.querySelector("#transcriptText"),
  connectButton: document.querySelector("#connectButton"),
  refreshButton: document.querySelector("#refreshButton"),
  sendTranscriptButton: document.querySelector("#sendTranscriptButton"),
  upButton: document.querySelector("#upButton"),
  clickButton: document.querySelector("#clickButton"),
  downButton: document.querySelector("#downButton"),
  backButton: document.querySelector("#backButton"),
};

elements.bridgeUrl.value = params.get("bridge") || savedBridgeUrl || DEFAULT_BRIDGE_URL;
elements.bridgeToken.value = params.get("token") || savedToken || "";

const logs = [];
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
  const bridgeUrl = elements.bridgeUrl.value.trim();
  const token = elements.bridgeToken.value.trim();
  window.localStorage.setItem("morpheus:g2:bridgeUrl", bridgeUrl);
  window.localStorage.setItem("morpheus:g2:token", token);
  client.configure({ bridgeUrl, token });
}

function render(snapshot) {
  const text = snapshot.glassesText || "Morpheus G2 simulator";
  elements.glassesText.textContent = text;
  pendingRender = pendingRender.then(() => renderEvenDisplay(text)).catch((err) => {
    log(`Even display render failed: ${err.message}`);
  });
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
    await renderEvenDisplay(client.snapshot().glassesText);
  } catch (err) {
    log(`Even SDK unavailable: ${err.message}`);
  }
}

async function handleEvenEvent(eventType) {
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
  await runAction(() => client.activateSelected());
}

async function runAction(action) {
  setBusy(true);
  try {
    await action();
  } catch (err) {
    client.state = { ...client.state, error: err.message };
    client.emit();
    log(err.message);
  } finally {
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

render(client.snapshot());
void initEvenDisplay();
