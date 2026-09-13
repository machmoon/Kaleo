import { useEffect } from "react";
import { BrowserRouter as Router, Routes, Route } from "react-router-dom";
import {
  Kaleo,
  Workbench,
  Engine,
  Integrations,
  Console,
  Settings,
  Shortcuts,
} from "@/pages";
import { DashboardLayout } from "@/layouts";
import Welcome from "@/pages/welcome";
import { RunProvider, useSilkscreenRun } from "@/contexts";
import {
  loadEngineBaseUrl,
  loadEngineToken,
} from "@/pages/engine/components/EngineConnection";
import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";

/**
 * Keep this window's engine address and token in step with the other window.
 *
 * Each webview seeds its provider from storage once, at creation — so before
 * this component existed, saving a new address on the dashboard's Engine page
 * left the overlay POSTing at the old one forever. The `storage` event fires
 * in every window except the writer, and the loaders re-validate on read, so
 * a hand-edited key still cannot smuggle in a non-loopback address.
 */
function EngineSettingsSync() {
  const run = useSilkscreenRun();
  useEffect(() => {
    const handler = (event: StorageEvent) => {
      if (event.key === KALEO_STORAGE_KEYS.ENGINE_BASE_URL) {
        run.setBaseUrl(loadEngineBaseUrl());
      }
      if (event.key === KALEO_STORAGE_KEYS.ENGINE_TOKEN) {
        run.setToken(loadEngineToken());
      }
    };
    window.addEventListener("storage", handler);
    return () => window.removeEventListener("storage", handler);
  }, [run]);
  return null;
}

export default function AppRoutes() {
  return (
    <Router>
      {/* RunProvider sits above both window trees: the overlay (/) starts
          runs, and the dashboard's workbench reads them. The two windows are
          separate webviews, so each gets its own provider instance; finished
          runs cross between them over the storage bridge
          (lib/silkscreen/bridge.ts), and engine settings via
          EngineSettingsSync below. */}
      {/* Seed from the persisted (and re-validated on read) engine address,
          so runs target what the Engine page says they target. */}
      <RunProvider baseUrl={loadEngineBaseUrl()} token={loadEngineToken()}>
      <EngineSettingsSync />
      <Routes>
        <Route path="/" element={<Kaleo />} />
        {/* The Setup Assistant: dashboard window, no sidebar. Its step is
            store state, not a sub-route, so a reload resumes in place. */}
        <Route path="/welcome" element={<Welcome />} />
        <Route element={<DashboardLayout />}>
          <Route path="/workbench" element={<Workbench />} />
          <Route path="/engine" element={<Engine />} />
          <Route path="/integrations" element={<Integrations />} />
          <Route path="/console" element={<Console />} />
          <Route path="/shortcuts" element={<Shortcuts />} />
          <Route path="/settings" element={<Settings />} />
        </Route>
      </Routes>
      </RunProvider>
    </Router>
  );
}
