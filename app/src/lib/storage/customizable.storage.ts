import { STORAGE_KEYS } from "@/config";

export interface CustomizableState {
  appIcon: {
    isVisible: boolean;
  };
  alwaysOnTop: {
    isEnabled: boolean;
  };
  autostart: {
    isEnabled: boolean;
  };
  /**
   * The "Ada" wake word. Off by default: on macOS the paid path is one
   * Gemini transcript per click, not always-on spotting. The ear and
   * Settings share this switch; the mic button is the reliable voice path.
   */
  wakeWord: {
    isEnabled: boolean;
  };
}

// Ada is a control strip floating over KiCad, so it stays on
// top by default: a stage that launches KiCad would otherwise bury it.
export const DEFAULT_CUSTOMIZABLE_STATE: CustomizableState = {
  appIcon: { isVisible: true },
  alwaysOnTop: { isEnabled: true },
  autostart: { isEnabled: true },
  wakeWord: { isEnabled: false },
};

// Bumped when a default changes in a way stored state must not keep
// overriding. Version 2: always-on-top became the default. Version 3: the
// wake word became the default (off was the conservative first ship).
// Version 4: wake word off again — Mac Gemini spotting is not always-on.
export const CUSTOMIZABLE_VERSION = 4;

/**
 * Get customizable state from localStorage
 */
export const getCustomizableState = (): CustomizableState => {
  try {
    const stored = localStorage.getItem(STORAGE_KEYS.CUSTOMIZABLE);
    if (!stored) {
      return DEFAULT_CUSTOMIZABLE_STATE;
    }

    const parsedState = JSON.parse(stored);
    const current = parsedState.version === CUSTOMIZABLE_VERSION;

    return {
      appIcon: parsedState.appIcon || DEFAULT_CUSTOMIZABLE_STATE.appIcon,
      alwaysOnTop:
        (current && parsedState.alwaysOnTop) ||
        DEFAULT_CUSTOMIZABLE_STATE.alwaysOnTop,
      autostart: parsedState.autostart || DEFAULT_CUSTOMIZABLE_STATE.autostart,
      wakeWord:
        (current && parsedState.wakeWord) ||
        DEFAULT_CUSTOMIZABLE_STATE.wakeWord,
    };
  } catch (error) {
    console.error("Failed to get customizable state:", error);
    return DEFAULT_CUSTOMIZABLE_STATE;
  }
};

/**
 * Save customizable state to localStorage
 */
export const setCustomizableState = (state: CustomizableState): void => {
  try {
    localStorage.setItem(
      STORAGE_KEYS.CUSTOMIZABLE,
      JSON.stringify({ ...state, version: CUSTOMIZABLE_VERSION })
    );
  } catch (error) {
    console.error("Failed to save customizable state:", error);
  }
};

/**
 * Update app icon visibility
 */
export const updateAppIconVisibility = (
  isVisible: boolean
): CustomizableState => {
  const currentState = getCustomizableState();
  const newState = { ...currentState, appIcon: { isVisible } };
  setCustomizableState(newState);
  return newState;
};

/**
 * Update always on top state
 */
export const updateAlwaysOnTop = (isEnabled: boolean): CustomizableState => {
  const currentState = getCustomizableState();
  const newState = { ...currentState, alwaysOnTop: { isEnabled } };
  setCustomizableState(newState);
  return newState;
};

/**
 * Update autostart state
 */
export const updateAutostart = (isEnabled: boolean): CustomizableState => {
  const currentState = getCustomizableState();
  const newState = { ...currentState, autostart: { isEnabled } };
  setCustomizableState(newState);
  return newState;
};

/**
 * Update the wake-word switch
 */
export const updateWakeWord = (isEnabled: boolean): CustomizableState => {
  const currentState = getCustomizableState();
  const newState = { ...currentState, wakeWord: { isEnabled } };
  setCustomizableState(newState);
  return newState;
};
