import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { invoke } from "@tauri-apps/api/core";
import { Button, Header } from "@/components";
import { setSetting } from "@/lib/settings/store";

/**
 * Re-run the Setup Assistant from the appearance step.
 *
 * `setup_restart` puts the shell back in setup mode (Regular activation
 * policy, dashboard focused; the strip stays visible). The store's
 * `setup.completed` goes false first so the wizard renders and so a window
 * closed mid-way is finished by the shell the same way a first run is.
 */
export const RunSetupAgain = () => {
  const navigate = useNavigate();
  const [note, setNote] = useState("");

  const run = async () => {
    setNote("");
    await setSetting("setup.completed", false);
    try {
      await invoke("setup_restart");
    } catch (error) {
      // Outside the shell there is nothing to restart; the wizard still runs.
      setNote(`The shell did not answer setup_restart (${(error as Error)?.message ?? "no shell"}); opening setup anyway.`);
    }
    navigate("/welcome?jump=appearance");
  };

  return (
    <div id="run-setup-again" className="space-y-3" data-testid="run-setup-again-settings">
      <Header
        isMainTitle
        title="Setup Assistant"
        description="Walk through appearance, engine, accounts and permissions again. Nothing is disconnected by running it."
      />
      <div className="flex items-center gap-3">
        <Button size="sm" variant="outline" onClick={() => void run()} data-testid="run-setup-again">
          Run Setup Again
        </Button>
        {note ? <span className="text-xs text-muted-foreground">{note}</span> : null}
      </div>
    </div>
  );
};
