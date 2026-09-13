import {
  Appearance,
  AlwaysOnTopToggle,
  AppIconToggle,
  AutostartToggle,
  WakeWordToggle,
  OverlaySkin,
  CliTools,
  BillingSetup,
  Notifications,
  RunSetupAgain,
} from "./components";
import { PageLayout } from "@/layouts";
import {
  loadEngineBaseUrl,
  loadEngineToken,
} from "@/pages/engine/components/EngineConnection";

const Settings = () => {
  return (
    <PageLayout title="Settings" description="Manage your settings">
      <Appearance />
      <OverlaySkin />
      <CliTools />
      <Notifications />
      <WakeWordToggle />
      <AutostartToggle />
      <AppIconToggle />
      <AlwaysOnTopToggle />
      <BillingSetup baseUrl={loadEngineBaseUrl()} token={loadEngineToken()} />
      <RunSetupAgain />
    </PageLayout>
  );
};

export default Settings;
