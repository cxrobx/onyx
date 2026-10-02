/**
 * Transport to the local Onyx service.
 *
 * The service is gated on Host + Origin + a per-process token. Obsidian's
 * renderer sends `Origin: app://obsidian.md`, which the service allows through
 * its `allowed_origins` setting; the token comes from `GET /api/session` and is
 * held in memory only, never written into the plugin's data.json.
 */

import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir, userInfo } from "node:os";
import { join } from "node:path";

import { SseSplitter, type SseFrame } from "./sse";
import type { MarkdownThemeSnapshot } from "./markdown-theme";
import type { SidebarThemeSnapshot } from "./sidebar-theme";

export type ServiceErrorKind = "offline" | "incompatible" | "forbidden" | "timeout" | "aborted" | "error";

export class ServiceError extends Error {
  readonly kind: ServiceErrorKind;

  constructor(kind: ServiceErrorKind, message: string) {
    super(message);
    this.name = "ServiceError";
    this.kind = kind;
  }
}

export interface Session {
  token: string;
  provider: string;
  model: string;
  reasoning_effort: string;
  first_activity_timeout: number;
  request_timeout: number;
  version: string;
}

export interface AskRequest {
  action: "eli5" | "prove" | "ask";
  selection: string;
  context?: string;
  question?: string;
  folder: string;
  document_source: string;
  document_title?: string;
  history?: { role: "user" | "assistant"; text: string }[];
  request_mode?: "generated" | "rerun" | "edited" | "continue";
  parent_request_id?: string;
}

export interface AskHandlers {
  onFrame(frame: SseFrame): void;
  signal?: AbortSignal;
}

export interface Citation {
  kind?: string;
  label?: string;
  path?: string;
  line?: number | null;
  page?: number | null;
  snippet?: string;
}

const TOKEN_ERRORS = ["Refused: invalid or missing token.", "invalid token"];
const BUNDLE_ID = "com.cx.onyx";
const DAEMON_LABEL = "com.cx.onyx.server";
const DAEMON_SCRIPT = join(homedir(), ".local", "bin", "onyx-daemon");

export class AskService {
  private session: Session | null = null;
  /** Set while a start attempt is in flight, so parallel actions wait on one. */
  private starting: Promise<boolean> | null = null;

  constructor(private baseUrl: string) {}

  setBaseUrl(url: string): void {
    const next = url.replace(/\/+$/, "");
    if (next !== this.baseUrl) {
      this.baseUrl = next;
      this.session = null;
    }
  }

  get url(): string {
    return this.baseUrl;
  }

  private async json<T>(path: string, init?: RequestInit): Promise<T> {
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, init);
    } catch (error) {
      throw new ServiceError("offline", `Onyx isn't reachable at ${this.baseUrl}.`);
    }
    if (response.status === 403) {
      throw new ServiceError("forbidden", "Onyx refused this request (origin or token).");
    }
    if (response.status === 404) {
      throw new ServiceError("incompatible", "This Onyx version is too old for the plugin.");
    }
    if (!response.ok) {
      throw new ServiceError("error", `Onyx returned HTTP ${response.status}.`);
    }
    return (await response.json()) as T;
  }

  async health(): Promise<{ service: string; protocol: number; version: string }> {
    const body = await this.json<{ service?: string; protocol?: number; version?: string }>("/health");
    if (body.service !== "onyx") {
      throw new ServiceError("incompatible", `Port in use by another service (${body.service ?? "unknown"}).`);
    }
    return { service: body.service, protocol: body.protocol ?? 0, version: body.version ?? "" };
  }

  /** Fetch (and cache) the request token plus the current provider/model.
   *
   * When nothing is listening, start the background service and try once more,
   * so an action in Obsidian works with the app closed.
   */
  async ensureSession(force = false): Promise<Session> {
    if (this.session && !force) return this.session;
    try {
      await this.health();
    } catch (error) {
      if (!(error instanceof ServiceError) || error.kind !== "offline") throw error;
      if (!(await this.autoStart())) {
        throw new ServiceError("offline", `Onyx could not be started at ${this.baseUrl}.`);
      }
      await this.health();
    }
    const body = await this.json<Session & { ok?: boolean }>("/api/session");
    if (!body.token) {
      throw new ServiceError("incompatible", "Onyx did not return a session token.");
    }
    this.session = body;
    return body;
  }

  /** Stream an answer. Frames are delivered in order; resolves when done. */
  async ask(request: AskRequest, handlers: AskHandlers, retry = true): Promise<void> {
    const session = await this.ensureSession();
    const controller = new AbortController();
    const abort = () => controller.abort();
    handlers.signal?.addEventListener("abort", abort, { once: true });
    // The service heartbeats every ~8s; give the whole request its own ceiling.
    const total = setTimeout(() => controller.abort(), (session.request_timeout + 15) * 1000);
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...request, token: session.token }),
        signal: controller.signal,
      });
    } catch (error) {
      clearTimeout(total);
      handlers.signal?.removeEventListener("abort", abort);
      if (handlers.signal?.aborted) throw new ServiceError("aborted", "Stopped.");
      throw new ServiceError("offline", `Onyx isn't reachable at ${this.baseUrl}.`);
    }

    if (response.status === 403 && retry) {
      clearTimeout(total);
      handlers.signal?.removeEventListener("abort", abort);
      this.session = null;
      await this.ensureSession(true);
      return this.ask(request, handlers, false);
    }
    if (!response.ok || !response.body) {
      clearTimeout(total);
      handlers.signal?.removeEventListener("abort", abort);
      throw new ServiceError("error", `Onyx returned HTTP ${response.status}.`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const splitter = new SseSplitter();
    let staleToken = false;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        for (const frame of splitter.push(decoder.decode(value, { stream: true }))) {
          if (frame.event === "error" && TOKEN_ERRORS.includes(String(frame.data.message ?? ""))) {
            staleToken = true;
            continue;
          }
          handlers.onFrame(frame);
        }
      }
      for (const frame of splitter.flush()) handlers.onFrame(frame);
    } catch (error) {
      if (handlers.signal?.aborted) throw new ServiceError("aborted", "Stopped.");
      if (controller.signal.aborted) throw new ServiceError("timeout", "Onyx stopped responding.");
      throw new ServiceError("error", error instanceof Error ? error.message : String(error));
    } finally {
      clearTimeout(total);
      handlers.signal?.removeEventListener("abort", abort);
      reader.cancel().catch(() => {});
    }

    if (staleToken && retry) {
      // The service restarted mid-flight and minted a new token.
      this.session = null;
      await this.ensureSession(true);
      return this.ask(request, handlers, false);
    }
    if (staleToken) {
      throw new ServiceError("forbidden", "Onyx refused the request token.");
    }
  }

  async history(source: string, selection = "", limit = 10): Promise<Record<string, unknown>[]> {
    const params = new URLSearchParams({ source, selection, limit: String(limit) });
    const body = await this.json<{ conversations?: Record<string, unknown>[] }>(
      `/api/history?${params.toString()}`,
    );
    return body.conversations ?? [];
  }

  async openSource(citation: Citation, folder: string): Promise<void> {
    const session = await this.ensureSession();
    await this.json("/api/open-source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: session.token,
        path: citation.path,
        line: citation.line ?? undefined,
        page: citation.page ?? undefined,
        folder,
      }),
    });
  }

  async validateFolder(path: string): Promise<boolean> {
    try {
      await this.json(`/api/folder?path=${encodeURIComponent(path)}`);
      return true;
    } catch {
      return false;
    }
  }

  async ensureRoot(path: string): Promise<void> {
    const session = await this.ensureSession();
    await this.json("/api/roots", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: session.token, path }),
    });
  }

  /**
   * Passive sync must never launch/focus the app. A fresh token also handles restarts. `other` is the vault's other
   * colour mode, for Onyx's Color theme setting; an older Onyx ignores it.
   */
  async syncMarkdownTheme(vaultRoot: string, snapshot: MarkdownThemeSnapshot, other?: MarkdownThemeSnapshot): Promise<void> {
    const signal = AbortSignal.timeout(5000);
    const session = await this.json<Session>("/api/session", { signal });
    if (!session.token) throw new ServiceError("incompatible", "Update Onyx to sync Markdown appearance.");
    await this.json("/api/markdown-theme", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: session.token, vault_root: vaultRoot, snapshot, ...(other ? { other } : {}) }),
      signal,
    });
  }

  /** The file explorer's look, for Onyx's vault sidebar. Passive like the reading theme. */
  async syncSidebarTheme(vaultRoot: string, snapshot: SidebarThemeSnapshot, other?: SidebarThemeSnapshot): Promise<void> {
    const signal = AbortSignal.timeout(5000);
    const session = await this.json<Session>("/api/session", { signal });
    if (!session.token) throw new ServiceError("incompatible", "Update Onyx to sync the sidebar appearance.");
    await this.json("/api/sidebar-theme", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: session.token, vault_root: vaultRoot, snapshot, ...(other ? { other } : {}) }),
      signal,
    });
  }

  /** Launch (or focus) the native app, optionally on a specific document. */
  openInApp(path?: string): void {
    const args = ["-b", BUNDLE_ID];
    if (path) args.push(path);
    execFile("/usr/bin/open", args, () => {});
  }

  /** Whether the background LaunchAgent is installed on this machine. */
  hasDaemon(): boolean {
    return existsSync(DAEMON_SCRIPT);
  }

  /**
   * Bring the service up without opening the app.
   *
   * Preferred route is the LaunchAgent, which runs the bundled server headless;
   * `kickstart` starts it if it is loaded but not running and is a no-op when it
   * already is. Falls back to running the daemon script directly, then to
   * launching the GUI app, so a machine with no agent installed still works.
   */
  private spawnService(): void {
    const uid = userInfo().uid;
    execFile("/bin/launchctl", ["kickstart", `gui/${uid}/${DAEMON_LABEL}`], (error) => {
      if (!error) return;
      if (this.hasDaemon()) {
        const child = execFile(DAEMON_SCRIPT, { env: process.env }, () => {});
        child.unref?.();
        return;
      }
      this.openInApp();
    });
  }

  /** Start the service and wait for it to answer. Concurrent calls share one. */
  async autoStart(timeoutMs = 12_000): Promise<boolean> {
    if (this.starting) return this.starting;
    this.starting = (async () => {
      this.spawnService();
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 400));
        try {
          await this.health();
          this.session = null;
          return true;
        } catch {
          // keep polling until the deadline
        }
      }
      return false;
    })();
    try {
      return await this.starting;
    } finally {
      this.starting = null;
    }
  }

  /** Back-compat alias used by the panel's manual "Open the app" button. */
  async waitForService(timeoutMs = 10_000): Promise<boolean> {
    return this.autoStart(timeoutMs);
  }
}
