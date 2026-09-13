import React from "react";
import ReactDOM from "react-dom/client";
import { AppProvider, ThemeProvider } from "./contexts";
import "./global.css";
import AppRoutes from "./routes";
import { initSettings } from "@/lib/settings/store";

// Hydrate the settings store (settings.json via the store plugin) and run the
// one-time existing-user check. Deliberately not awaited: the localStorage
// mirror answers the first paint synchronously, and the plugin round trip
// must not delay the strip.
void initSettings();

// Only two windows exist now (the Kaleo bar and the dashboard); both render
// the same app and route by URL. The capture-overlay-* branch died with
// capture.rs -- nothing creates those windows anymore.
{
  ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
    <React.StrictMode>
      <ThemeProvider>
        <AppProvider>
          <AppRoutes />
        </AppProvider>
      </ThemeProvider>
    </React.StrictMode>
  );
}
