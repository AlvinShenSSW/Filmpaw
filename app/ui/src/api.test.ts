import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./client/client.gen", () => ({ client: { setConfig: vi.fn() } }));

import rsbuildConfig from "../rsbuild.config";
import { configureClient, sessionHeaders } from "./api";
import { client } from "./client/client.gen";

afterEach(() => {
  delete window.__FILMPAW_SESSION_TOKEN__;
  delete window.__FILMPAW_PORT__;
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.clearAllMocks();
});

describe("sidecar session credential", () => {
  it("uses the shell credential ahead of a standalone development credential", () => {
    vi.stubGlobal("__FILMPAW_DEV_TOKEN__", "b".repeat(64));
    window.__FILMPAW_SESSION_TOKEN__ = "a".repeat(64);
    expect(sessionHeaders()).toEqual({ "X-Filmpaw-Session": "a".repeat(64) });
  });

  it("supports a development credential without an injected shell", () => {
    vi.stubGlobal("__FILMPAW_DEV_TOKEN__", "b".repeat(64));
    expect(sessionHeaders()).toEqual({ "X-Filmpaw-Session": "b".repeat(64) });
  });

  it("preserves uppercase hexadecimal development credentials exactly", () => {
    const token = "AbCdEf0123456789".repeat(4);
    vi.stubGlobal("__FILMPAW_DEV_TOKEN__", token);
    expect(sessionHeaders()).toEqual({ "X-Filmpaw-Session": token });
  });

  it("does not send missing, malformed, or header-injection credentials", () => {
    vi.stubGlobal("__FILMPAW_DEV_TOKEN__", "");
    expect(sessionHeaders()).toEqual({});
    window.__FILMPAW_SESSION_TOKEN__ = "bad\r\nOther: value";
    expect(sessionHeaders()).toEqual({});
  });

  it("configures the generated SDK with the same credential and port", () => {
    window.__FILMPAW_PORT__ = 12345;
    window.__FILMPAW_SESSION_TOKEN__ = "c".repeat(64);
    configureClient();
    expect(client.setConfig).toHaveBeenCalledWith({
      baseUrl: "http://127.0.0.1:12345",
      headers: { "X-Filmpaw-Session": "c".repeat(64) },
    });
  });

  it("never embeds a development credential in a built bundle", () => {
    vi.stubEnv("FILMPAW_SESSION_TOKEN", "d".repeat(64));
    for (const env of ["production", "development"]) {
      const config = rsbuildConfig({ env, command: "build" });
      expect(config.source?.define?.__FILMPAW_DEV_TOKEN__).toBe('""');
    }
    const dev = rsbuildConfig({ env: "development", command: "dev" });
    expect(dev.source?.define?.__FILMPAW_DEV_TOKEN__).toBe(JSON.stringify("d".repeat(64)));
  });
});
