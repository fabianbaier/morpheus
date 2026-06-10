// Morpheus desktop — preload bridge.
//
// The renderer talks to the Python bridge over same-origin HTTP/SSE (it already
// has the token in the URL), so we expose only a tiny, safe surface here rather
// than any Node/IPC power. contextIsolation is on; nodeIntegration is off.

const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("morpheus", {
  platform: process.platform,
  isDesktopShell: true,
  version: process.env.npm_package_version || "dev",
});
