export const STORAGE_KEYS = Object.freeze({
  bridgeUrl: "morpheus:g2:bridgeUrl",
  token: "morpheus:g2:token",
  skin: "morpheus:g2:skin",
});

export function readStorage(windowRef, storageName, key) {
  try {
    return windowRef?.[storageName]?.getItem(key) || "";
  } catch {
    return "";
  }
}

export function writeStorage(windowRef, storageName, key, value) {
  try {
    windowRef?.[storageName]?.setItem(key, value);
  } catch {}
}

export function removeStorage(windowRef, storageName, key) {
  try {
    windowRef?.[storageName]?.removeItem(key);
  } catch {}
}

export function storedBridgeUrl(windowRef) {
  return readStorage(windowRef, "localStorage", STORAGE_KEYS.bridgeUrl);
}

export function storedSkin(windowRef) {
  return readStorage(windowRef, "localStorage", STORAGE_KEYS.skin);
}

export function storedToken(windowRef, params) {
  if (params?.has?.("token")) return params.get("token") || "";
  return readStorage(windowRef, "sessionStorage", STORAGE_KEYS.token);
}

export function clearLegacyTokenStorage(windowRef) {
  removeStorage(windowRef, "localStorage", STORAGE_KEYS.token);
}

export function saveBridgeConfig(windowRef, { bridgeUrl, token }) {
  writeStorage(windowRef, "localStorage", STORAGE_KEYS.bridgeUrl, bridgeUrl);
  writeStorage(windowRef, "sessionStorage", STORAGE_KEYS.token, token);
  clearLegacyTokenStorage(windowRef);
}

export function savePreviewSkin(windowRef, skin) {
  writeStorage(windowRef, "localStorage", STORAGE_KEYS.skin, skin);
}
