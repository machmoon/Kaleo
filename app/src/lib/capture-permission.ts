// Capture permissions for Ada.
//
// Spoken “hey Ada” needs the microphone. We may ask the OS permission dialog
// once; we never yank the user into System Settings on every arm — if they
// already denied, the ear shows where to flip it and they open Settings
// themselves.

export type CaptureAccess = "granted" | "denied" | "unavailable";

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function macosMicGranted(): Promise<boolean | null> {
  try {
    const { checkMicrophonePermission } = await import(
      "tauri-plugin-macos-permissions-api"
    );
    return await checkMicrophonePermission();
  } catch {
    return null;
  }
}

async function macosRequestMic(): Promise<void> {
  try {
    const { requestMicrophonePermission } = await import(
      "tauri-plugin-macos-permissions-api"
    );
    await requestMicrophonePermission();
  } catch {
    // Fall through to getUserMedia.
  }
}

/**
 * Hard requirement for spoken wake / push-to-talk: native mic dialog (once)
 * + a getUserMedia probe. Throws with a Settings *hint* when refused — does
 * not open System Settings.
 */
export async function ensureSpokenAudio(): Promise<CaptureAccess> {
  const native = await macosMicGranted();
  if (native === false) {
    await macosRequestMic();
    for (let i = 0; i < 40; i += 1) {
      await sleep(250);
      if (await macosMicGranted()) break;
    }
    if ((await macosMicGranted()) === false) {
      throw new Error(
        "Microphone is off for Hardy. Enable it in System Settings → Privacy & Security → Microphone, then click the ear again."
      );
    }
  }

  const gum = globalThis.navigator?.mediaDevices?.getUserMedia;
  if (typeof gum !== "function") {
    throw new Error("this environment has no microphone access");
  }

  try {
    const stream = await gum.call(globalThis.navigator.mediaDevices, {
      audio: true,
    });
    for (const track of stream.getTracks()) {
      track.stop();
      track.enabled = false;
    }
    return "granted";
  } catch (error) {
    const message = (error as Error)?.message || "permission denied";
    if (/denied|not.?allowed|permission/i.test(message)) {
      throw new Error(
        "Microphone permission was refused. Enable Hardy in System Settings → Privacy & Security → Microphone, then click the ear again."
      );
    }
    throw new Error(`could not use the microphone: ${message}`);
  }
}

/** Arm spoken wake — microphone only; no Settings pane, no desk-audio side trips. */
export async function ensureCaptureAccess(): Promise<CaptureAccess> {
  return ensureSpokenAudio();
}

/** @deprecated Prefer ensureCaptureAccess. */
export async function ensureMicrophoneAccess(): Promise<CaptureAccess> {
  return ensureCaptureAccess();
}
