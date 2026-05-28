// Morpheus desktop — Electron main process.
//
// Responsibilities (kept deliberately thin):
//   1. Spawn the Python bridge:  `morpheus desktop serve --handshake`
//   2. Read the single JSON handshake line {host, port, token, url} from its
//      stdout to learn where to point the window and which auth token to use.
//   3. Open a native BrowserWindow at that URL.
//   4. Own the child lifecycle: kill it on quit / crash so no orphan server
//      keeps holding the port and a DB connection.
//
// The Python bridge picks an OS-assigned port (0) and is the single source of
// truth for the auth token, so there is nothing to configure or keep in sync.

const { app, BrowserWindow, shell, Menu } = require("electron");
const { spawn } = require("node:child_process");
const readline = require("node:readline");

// Allow overriding how the bridge is launched (e.g. a venv path) via env.
const MORPHEUS_BIN = process.env.MORPHEUS_BIN || "morpheus";
const MORPHEUS_ARGS = ["desktop", "serve", "--handshake"];

let child = null;
let mainWindow = null;

function startBridge() {
  return new Promise((resolve, reject) => {
    child = spawn(MORPHEUS_BIN, MORPHEUS_ARGS, {
      stdio: ["ignore", "pipe", "pipe"],
      env: process.env,
    });

    child.on("error", (err) =>
      reject(new Error(`Failed to launch '${MORPHEUS_BIN}': ${err.message}`)));
    child.on("exit", (code) => {
      child = null;
      if (mainWindow === null) reject(new Error(`bridge exited early (code ${code})`));
    });

    // Parse the first stdout line as the handshake JSON.
    const rl = readline.createInterface({ input: child.stdout });
    const timeout = setTimeout(() => reject(new Error("bridge handshake timed out")), 15000);
    rl.on("line", (line) => {
      try {
        const info = JSON.parse(line);
        if (info && info.url) {
          clearTimeout(timeout);
          rl.close();
          resolve(info);
        }
      } catch (_) {
        // Non-JSON log line before the handshake — ignore and keep reading.
      }
    });

    child.stderr.on("data", (d) => process.stderr.write(`[bridge] ${d}`));
  });
}

function createWindow(info) {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 560,
    backgroundColor: "#0B0F0C",
    titleBarStyle: "hiddenInset", // native traffic lights over our custom toolbar
    trafficLightPosition: { x: 16, y: 16 },
    webPreferences: {
      preload: require("node:path").join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadURL(info.url);

  // Open external links in the system browser, never in-app.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.on("closed", () => { mainWindow = null; });
}

function stopBridge() {
  if (child && !child.killed) {
    try { child.kill("SIGTERM"); } catch (_) {}
    child = null;
  }
}

app.whenReady().then(async () => {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { role: "appMenu" },
    { role: "editMenu" },
    { role: "viewMenu" },
    { role: "windowMenu" },
  ]));
  try {
    const info = await startBridge();
    createWindow(info);
  } catch (err) {
    const { dialog } = require("electron");
    dialog.showErrorBox(
      "Morpheus failed to start",
      `${err.message}\n\nMake sure 'morpheus' is installed and on PATH ` +
      `(pip install -e .), or set MORPHEUS_BIN to its full path.`,
    );
    app.quit();
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0 && child) {
      // Re-open a window against the still-running bridge.
      startBridge().then(createWindow).catch(() => app.quit());
    }
  });
});

app.on("window-all-closed", () => {
  stopBridge();
  if (process.platform !== "darwin") app.quit();
});
app.on("before-quit", stopBridge);
app.on("will-quit", stopBridge);
process.on("exit", stopBridge);
