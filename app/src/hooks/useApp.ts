import { useEffect, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import { getShortcutsConfig } from "@/lib/storage";
import { invoke } from "@tauri-apps/api/core";

/**
 * Window-level app wiring for the overlay: registers the global shortcuts on
 * startup, tracks the Windows hide/show workaround state (`isHidden`), and
 * surfaces shortcut-registration failures. The Pluely chat concerns that used
 * to live here (SQLite chat-history migration, conversation selection events,
 * system-audio capture, title stripping) left with the chat product.
 */
export const useApp = () => {
  const [isHidden, setIsHidden] = useState(false);

  // Initialize shortcuts from localStorage on app startup
  useEffect(() => {
    const initializeShortcuts = async () => {
      try {
        const config = getShortcutsConfig();
        await invoke("update_shortcuts", { config });
      } catch (error) {
        console.error("Failed to initialize shortcuts:", error);
      }
    };

    initializeShortcuts();
  }, []);

  // WINDOWS HIDE/SHOW TOGGLE WINDOW WORKAROUND FOR SHORTCUTS
  useEffect(() => {
    const unlistenPromise = listen<boolean>(
      "toggle-window-visibility",
      (event) => {
        const platform = navigator.platform.toLowerCase();
        if (typeof event.payload === "boolean" && platform.includes("win")) {
          setIsHidden(!event.payload);
          // find popover open and close it
          const popover = document.getElementById("popover-content");
          // set display to none, change data-state to closed
          if (popover) {
            popover.style.setProperty("display", "none", "important");
            // update the data-state to closed
            popover.setAttribute("data-state", "closed");

            // Also find and update the popover trigger's data-state
            const popoverTriggers = document.querySelectorAll(
              '[data-slot="popover-trigger"]'
            );
            popoverTriggers.forEach((trigger) => {
              trigger.setAttribute("data-state", "closed");
            });
          }
        }
      }
    );

    return () => {
      unlistenPromise.then((unlisten) => unlisten());
    };
  }, []);

  useEffect(() => {
    const handleShortcutRegistrationError = (
      event: Event | CustomEvent<Array<[string, string, string]>>
    ) => {
      const detail =
        (event as CustomEvent<Array<[string, string, string]>>)?.detail ?? [];

      if (!detail.length) {
        return;
      }

      const formatted = detail
        .map(([action, key, error]) => ({ action, key, error }))
        .filter(({ action, key }) => action && key);

      if (!formatted.length) {
        return;
      }

      console.warn(
        "Some shortcuts could not be registered:",
        formatted.map(({ action, key, error }) => ({
          action,
          key,
          error,
        }))
      );
    };

    window.addEventListener(
      "shortcutRegistrationError",
      handleShortcutRegistrationError as EventListener
    );

    return () => {
      window.removeEventListener(
        "shortcutRegistrationError",
        handleShortcutRegistrationError as EventListener
      );
    };
  }, []);

  return {
    isHidden,
    setIsHidden,
  };
};
