// Constants for Kaleo's own surfaces. Deliberately a separate file from
// `constants.ts`: that one is inherited Pluely configuration, and keeping the
// silkscreen keys out of it makes the fork's additions greppable.

/**
 * Storage keys, namespaced under `silkscreen_` so they cannot collide with the
 * Pluely keys sharing this localStorage.
 */
export const KALEO_STORAGE_KEYS = {
  ENGINE_BASE_URL: "silkscreen_engine_base_url",
  /**
   * The optional bearer token for an engine deployed behind a token gate.
   * localStorage on purpose: it is how this app already stores provider API
   * keys (no JS in this build touches the keychain plugin), and the engine is
   * loopback-only here anyway.
   */
  ENGINE_TOKEN: "silkscreen_engine_token",
  /**
   * The spoken-digest switch ("1"/"0", absent means on) and the optional
   * ElevenLabs credentials. The key is localStorage for the same reason the
   * engine token is; its value must never appear in a log or error string —
   * see `src/lib/speech/`.
   */
  VOICE_ENABLED: "silkscreen_voice_enabled",
  ELEVENLABS_KEY: "silkscreen_elevenlabs_key",
  ELEVENLABS_VOICE_ID: "silkscreen_elevenlabs_voice_id",
  /**
   * Opt in to the webview's own `speechSynthesis` as Ada's voice ("1"/"0",
   * absent means no).
   *
   * Absent-means-NO is the whole point, and it is the opposite of
   * `VOICE_ENABLED` above. The platform voice on macOS is a *Compact* system
   * voice, and for several weeks it was what Ada actually sounded like — not
   * because anyone chose it, but because the engine voice was never reached
   * and this was what the code fell through to. A degradation nobody can
   * hear the reason for is indistinguishable from a design decision, so the
   * platform voice is now only ever a thing someone asked for by name.
   */
  PLATFORM_VOICE_OPT_IN: "silkscreen_platform_voice",
  /**
   * The most recently finished run, serialized for the other window. The
   * overlay and the dashboard are separate webviews with separate providers;
   * this key plus the `storage` event is how a finished run crosses between
   * them — see `src/lib/silkscreen/bridge.ts`.
   */
  LAST_RUN: "silkscreen_last_run",
  /**
   * Which overlay skin is on, and whether the terminal skin's Ada routing is
   * live. Two keys rather than one blob: the skin is a preference people
   * change often, and the routing switch has to be readable from inside the
   * terminal component without dragging the whole settings shape in with it.
   */
  OVERLAY_SKIN: "silkscreen_overlay_skin",
  TERMINAL_ADA: "silkscreen_terminal_ada",
  /**
   * The localStorage mirror of the settings store (`src/lib/settings/`):
   * every `SettingsSchema` key is written as JSON under
   * `kaleo.settings.<key>`, so a synchronous read at first paint and the
   * cross-window `storage` event both work without waiting on the plugin.
   * Deliberately NOT under `silkscreen_`: the presence of any `silkscreen_*`
   * key is how `initSettings()` recognises an existing user, and the mirror
   * must not make a fresh install look like one.
   */
  SETTINGS_MIRROR_PREFIX: "kaleo.settings.",
  NOTIFY_ENABLED: "kaleo.settings.notify.enabled",
  NOTIFY_OS: "kaleo.settings.notify.os",
  SETUP_COMPLETED: "kaleo.settings.setup.completed",
  SETUP_VERSION: "kaleo.settings.setup.version",
  TOUR_COMPLETED: "kaleo.settings.tour.completed",
} as const;

/**
 * The `~/.kaleo/*.env` family: where a desktop install keeps what Setup saved.
 *
 * This is the repository's existing answer to "read a credential from the
 * environment", and it is the answer because a **Tauri webview cannot read
 * process env at all** — there is no `process`, and `std::env` lives in Rust.
 * `service/envfiles.py` already writes and reads exactly this shape for
 * `billing`, `google` and `microsoft`: one flat `KEY=value` file per provider,
 * 0600 under a 0700 `~/.kaleo`, applied at start with **setdefault**
 * semantics so a real process-environment value always wins over a file.
 *
 * `voice` is the fourth file, and it carries `ELEVENLABS_API_KEY` — the name
 * ElevenLabs' own SDKs read, so a developer who has already exported it can
 * `echo "ELEVENLABS_API_KEY=$ELEVENLABS_API_KEY" > ~/.kaleo/voice.env` and be
 * done. The service half (registering `voice` in `envfiles.FILES` so the
 * Setup Assistant can write it) is a one-line follow-up in a file this change
 * does not own; the file works today whether or not that lands, because the
 * app reads it directly.
 *
 * Path, not `BaseDirectory`-relative-to-anything-clever: it is read through
 * the fs plugin's `$HOME` base, and the capability files scope
 * `$HOME/.kaleo/*.env` for exactly this.
 */
export const KALEO_ENV_DIR = ".kaleo";
export const KALEO_VOICE_ENV_FILE = `${KALEO_ENV_DIR}/voice.env`;
/** The variable name inside that file. Never the value, obviously. */
export const ELEVENLABS_ENV_VAR = "ELEVENLABS_API_KEY";

/**
 * Hostnames the engine may live on.
 *
 * Loopback only, and the check is a hard refusal rather than a warning. The
 * service ships no authentication and no CORS headers by design, so a base URL
 * pointing anywhere else hands an unauthenticated `/generate` — which spends
 * the user's Gemini quota on every call — to whoever can reach that address.
 * `127.0.0.0/8` is matched by prefix because the whole block is loopback.
 */
export const LOOPBACK_HOSTS = ["localhost", "::1", "[::1]"] as const;

/**
 * How to start the engine, quoted from the repository's own docs rather than
 * paraphrased — a command that does not run is worse than no command.
 *
 * The `.env` line is the one that catches people: `service/app.py` reads the
 * process environment only and has no dotenv loader, so a service started
 * directly with a `.env` sitting next to it comes up keyless and answers
 * `/generate` with a 502 naming `GOOGLE_API_KEY`. `silkscreen serve` loads
 * `.env` itself, which is why it is listed first.
 */
export interface EngineStartStep {
  id: string;
  title: string;
  detail: string;
  command: string;
}

export const ENGINE_START_STEPS: EngineStartStep[] = [
  {
    id: "serve",
    title: "The easy way",
    detail:
      "`silkscreen serve` loads .env itself before starting the server, so the key is already in the environment. Run it from the repository checkout.",
    command: "silkscreen serve --port 8081",
  },
  {
    id: "module",
    title: "Running the service module directly",
    detail:
      "python -m service.app does NOT read .env — cli.py has the dotenv loader, service/app.py reads the process environment only. Export GOOGLE_API_KEY yourself or the engine comes up keyless and every run fails with a 502 naming that variable.",
    command: "set -a && . ./.env && set +a && PORT=8081 python -m service.app",
  },
  {
    id: "module-powershell",
    title: "The same thing on PowerShell",
    detail:
      "Windows equivalent of exporting the key before starting the module.",
    command: '$env:GOOGLE_API_KEY = "..."; $env:PORT = "8081"; python -m service.app',
  },
  {
    id: "install",
    title: "If nothing is installed yet",
    detail:
      "Create the venv and install the engine editable, from the repository root. The install is ~400 MB, almost all of it OR-Tools.",
    command:
      'python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev,agents,cloud,adk]"',
  },
];
