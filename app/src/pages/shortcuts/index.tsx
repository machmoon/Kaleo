import { ShortcutManager } from "./components";
import { PageLayout } from "@/layouts";

const Shortcuts = () => {
  return (
    <PageLayout
      title="Keyboard Shortcuts"
      description="Manage your keyboard shortcuts"
    >
      <div className="flex flex-col gap-6 pb-8">

        {/* Keyboard Shortcuts */}
        <ShortcutManager />
      </div>
    </PageLayout>
  );
};

export default Shortcuts;
