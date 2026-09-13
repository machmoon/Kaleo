import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { WelcomeLayout } from "@/layouts/WelcomeLayout";
import { useReducedMotion } from "@/hooks/useReducedMotion";
import { loadEngineBaseUrl, loadEngineToken } from "@/pages/engine/components/EngineConnection";
import { isSetupStepId, type SetupStepId } from "@/lib/setup/machine";
import { startTour } from "@/lib/tour";
import { useSetup } from "./useSetup";
import { useSetupReport } from "./useSetupReport";
import { HELLO_TITLE, HelloStep } from "./steps/HelloStep";
import { APPEARANCE_TITLE, AppearanceStep } from "./steps/AppearanceStep";
import { ENGINE_TITLE, EngineStep } from "./steps/EngineStep";
import { ACCOUNTS_TITLE, AccountsStep } from "./steps/AccountsStep";
import { PERMISSIONS_TITLE, PermissionsStep } from "./steps/PermissionsStep";
import { DONE_TITLE, DoneStep } from "./steps/DoneStep";

const TITLES: Record<SetupStepId, string> = {
  hello: HELLO_TITLE,
  appearance: APPEARANCE_TITLE,
  engine: ENGINE_TITLE,
  accounts: ACCOUNTS_TITLE,
  permissions: PERMISSIONS_TITLE,
  done: DONE_TITLE,
};

/** The outgoing step's fade (`--kv-dur-exit`); the incoming one settles after. */
const SWAP_OUT_MS = 110;

/**
 * `/welcome`: the Setup Assistant, rendered in the dashboard window with no
 * sidebar. The step is store state, not a sub-route, so a reload resumes and
 * Back/Continue never touch history.
 */
const Welcome = () => {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const setup = useSetup();
  const { state, dispatch } = setup;
  const baseUrl = loadEngineBaseUrl();
  const token = loadEngineToken();
  const report = useSetupReport(baseUrl, token);
  const still = useReducedMotion();
  const demo = report.report?.mode === "demo";

  // "Run Setup Again" arrives with ?jump=appearance: honour it once.
  const jumped = useRef(false);
  useEffect(() => {
    if (jumped.current) return;
    jumped.current = true;
    const jump = params.get("jump");
    if (isSetupStepId(jump)) dispatch({ type: "jump", step: jump });
  }, [params, dispatch]);

  // The store already says finished: this window has no business here.
  useEffect(() => {
    if (!setup.needsSetup && !params.get("jump")) navigate("/workbench", { replace: true });
  }, [setup.needsSetup, params, navigate]);

  // Crossfade presence: keep the outgoing step mounted for its fade, then
  // swap. Reduced motion swaps in one frame.
  const [shown, setShown] = useState<SetupStepId>(state.step);
  const [leaving, setLeaving] = useState(false);
  useEffect(() => {
    if (state.step === shown) return;
    if (still) {
      setShown(state.step);
      return;
    }
    setLeaving(true);
    const timer = window.setTimeout(() => {
      setShown(state.step);
      setLeaving(false);
    }, SWAP_OUT_MS);
    return () => window.clearTimeout(timer);
  }, [state.step, shown, still]);

  const [engineOk, setEngineOk] = useState(false);
  const canContinue = shown === "engine" ? engineOk : true;

  const leave = useCallback(
    async (tour: boolean) => {
      await setup.finish();
      if (tour) startTour("first-run");
      navigate("/workbench", { replace: true });
    },
    [setup, navigate],
  );

  // Shift+Esc confirmed: the machine says finished, so finish.
  const exiting = useRef(false);
  useEffect(() => {
    if (!state.finished || exiting.current) return;
    exiting.current = true;
    void leave(false);
  }, [state.finished, leave]);

  const onContinue = useCallback(() => dispatch({ type: "continue" }), [dispatch]);
  const onBack = useCallback(() => dispatch({ type: "back" }), [dispatch]);
  const onLater = useCallback(() => dispatch({ type: "skip" }), [dispatch]);
  const onExit = useCallback(() => dispatch({ type: "exit" }), [dispatch]);
  const onAutoAdvance = useCallback(() => dispatch({ type: "autoAdvance", from: "engine" }), [dispatch]);

  if (!setup.needsSetup && !params.get("jump")) return null;

  const step = (() => {
    switch (shown) {
      case "hello":
        return <HelloStep />;
      case "appearance":
        return <AppearanceStep />;
      case "engine":
        return (
          <EngineStep
            baseUrl={baseUrl}
            token={token}
            onCanContinue={setEngineOk}
            onAutoAdvance={onAutoAdvance}
            setCard={setup.setCard}
          />
        );
      case "accounts":
        return <AccountsStep baseUrl={baseUrl} token={token} report={report} setCard={setup.setCard} />;
      case "permissions":
        return <PermissionsStep setCard={setup.setCard} />;
      case "done":
        return (
          <DoneStep skipped={state.skipped} demo={demo} onTour={() => leave(true)} onNotNow={() => leave(false)} />
        );
    }
  })();

  return (
    <WelcomeLayout
      title={TITLES[shown]}
      index={setup.index}
      total={setup.total}
      demo={demo}
      showDots={shown !== "hello"}
      showFooter={shown !== "done"}
      showBack={shown !== "hello"}
      canContinue={canContinue}
      continueLabel={shown === "hello" ? "Continue" : shown === "permissions" ? "Finish" : "Continue"}
      onContinue={onContinue}
      onBack={onBack}
      onLater={onLater}
      onExit={onExit}
    >
      <div
        key={shown}
        className={`w-full ${leaving ? "kv-swap-out" : "kv-settle-group"}`}
        data-testid="setup-presence"
        data-leaving={leaving ? "true" : "false"}
      >
        {step}
      </div>
    </WelcomeLayout>
  );
};

export default Welcome;
