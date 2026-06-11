import assert from "node:assert/strict";
import test from "node:test";
import {
  STORAGE_KEYS,
  clearLegacyTokenStorage,
  readStorage,
  saveBridgeConfig,
  storedToken,
} from "../simulator/src/storage.js";

function memoryStorage(initial = {}) {
  const items = new Map(Object.entries(initial));
  return {
    getItem(key) {
      return items.has(key) ? items.get(key) : null;
    },
    setItem(key, value) {
      items.set(key, String(value));
    },
    removeItem(key) {
      items.delete(key);
    },
  };
}

test("simulator token prefers query params then session storage", () => {
  const windowRef = {
    localStorage: memoryStorage({ [STORAGE_KEYS.token]: "legacy-token" }),
    sessionStorage: memoryStorage({ [STORAGE_KEYS.token]: "session-token" }),
  };

  assert.equal(storedToken(windowRef, new URLSearchParams("token=query-token")), "query-token");
  assert.equal(storedToken(windowRef, new URLSearchParams("token=")), "");
  assert.equal(storedToken(windowRef, new URLSearchParams()), "session-token");
});

test("simulator startup purge removes legacy localStorage token", () => {
  const windowRef = {
    localStorage: memoryStorage({ [STORAGE_KEYS.token]: "legacy-token" }),
    sessionStorage: memoryStorage({ [STORAGE_KEYS.token]: "session-token" }),
  };

  clearLegacyTokenStorage(windowRef);

  assert.equal(windowRef.localStorage.getItem(STORAGE_KEYS.token), null);
  assert.equal(windowRef.sessionStorage.getItem(STORAGE_KEYS.token), "session-token");
});

test("simulator save writes token only to session storage", () => {
  const windowRef = {
    localStorage: memoryStorage({ [STORAGE_KEYS.token]: "legacy-token" }),
    sessionStorage: memoryStorage(),
  };

  saveBridgeConfig(windowRef, { bridgeUrl: "http://127.0.0.1:3456", token: "session-token" });

  assert.equal(windowRef.localStorage.getItem(STORAGE_KEYS.bridgeUrl), "http://127.0.0.1:3456");
  assert.equal(windowRef.localStorage.getItem(STORAGE_KEYS.token), null);
  assert.equal(windowRef.sessionStorage.getItem(STORAGE_KEYS.token), "session-token");
});

test("simulator storage helpers tolerate unavailable storage", () => {
  const windowRef = {
    get localStorage() {
      throw new Error("storage blocked");
    },
  };

  assert.equal(readStorage(windowRef, "localStorage", STORAGE_KEYS.token), "");
  assert.doesNotThrow(() => clearLegacyTokenStorage(windowRef));
});
