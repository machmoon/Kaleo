import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Button, Header } from "@/components";
import { EngineStartCommands } from "@/components/setup";
import { PageLayout } from "@/layouts";
import { useEngineHealth } from "@/hooks";
import { useSilkscreenRun } from "@/contexts/run.context";
import {
  EngineConnection,
  EngineStatus,
  VoiceSettings,
  loadEngineBaseUrl,
  loadEngineToken,
} from "./components";

/**
 * The engine page: point Kaleo at a running service, and prove it is alive.
 *
 * This page exists because "connection refused" is not a diagnosis. Every
 * other surface in the app fails the same opaque way when the Python service
 * is not running, and the service is a separate process the user starts by
 * hand — so somewhere has to say which address is in use, whether anything is
 * answering there, and what to type if nothing is.
 */
const Engine = () => {
  // Read once: a rerender must not reach back into storage and stomp a value
  // the user just changed.
  const navigate = useNavigate();
  // The wizard's "Use a different address" lands here with ?return=/welcome.
  const [params] = useSearchParams();
  const returnTo = params.get("return");
  const [baseUrl, setBaseUrl] = useState(loadEngineBaseUrl);
  const [token, setToken] = useState(loadEngineToken);
  // The poll restarts on its own when `baseUrl` changes — the hook keys on it.
  const health = useEngineHealth(baseUrl, undefined, token);
  // Saving here must reach the run layer too, or the address on this page and
  // the address runs actually target quietly diverge until the next launch.
  const run = useSilkscreenRun();
  const applyBaseUrl = (url: string) => {
    setBaseUrl(url);
    run.setBaseUrl(url);
  };
  const applyToken = (value: string) => {
    setToken(value);
    run.setToken(value);
  };

  return (
    <PageLayout
      title="Engine"
      description="The silkscreen service that generates boards"
      rightSlot={
        <div className="flex items-center gap-2">
          {returnTo === "/welcome" ? (
            <Button size="sm" variant="outline" onClick={() => navigate(returnTo)} data-testid="engine-return-setup">
              Back to setup
            </Button>
          ) : null}
          <EngineStatus health={health} showDetail />
          <Button
            size="sm"
            variant="outline"
            onClick={health.recheck}
            disabled={health.checking}
          >
            Recheck
          </Button>
        </div>
      }
    >
      <EngineConnection
        baseUrl={baseUrl}
        onBaseUrlChange={applyBaseUrl}
        token={token}
        onTokenChange={applyToken}
      />

      <VoiceSettings />

      <div id="engine-start" className="space-y-3">
        <Header
          title="Starting the engine"
          description="Hardy does not start or bundle the service — it is a separate Python process you run from the silkscreen checkout"
          isMainTitle
        />
        <EngineStartCommands />
        <p className="text-xs leading-relaxed text-muted-foreground">
          The API key caveat is not a footnote: only the CLI reads{" "}
          <code className="font-mono">.env</code>.{" "}
          <code className="font-mono">cli.py</code> has a small dotenv loader
          and <code className="font-mono">service/app.py</code> does not — it
          reads the process environment only. A service started directly next to
          a filled-in <code className="font-mono">.env</code> therefore comes up
          with no key and answers every run with a 502 naming{" "}
          <code className="font-mono">GOOGLE_API_KEY</code>.
        </p>
      </div>

      <div id="engine-boundary" className="space-y-2">
        <Header
          title="Why loopback only"
          description="The constraint the address field enforces"
          isMainTitle
        />
        <p className="text-xs leading-relaxed text-muted-foreground">
          The service ships no authentication and sends no CORS headers, on
          purpose — it assumes it is reachable only from the machine it runs on.
          Every run it accepts spends your Gemini quota. So Hardy refuses any
          address that is not <code className="font-mono">http://</code> on
          loopback: accepting one would put an open, billable endpoint on the
          network, and neither this app nor the service would notice.
        </p>
      </div>
    </PageLayout>
  );
};

export default Engine;
