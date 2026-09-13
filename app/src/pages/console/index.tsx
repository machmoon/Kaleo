import { Header } from "@/components";
import { DebugConsole } from "@/components/debug";

/**
 * Full-page log surface. Other dashboard routes go through PageLayout's
 * ScrollArea; the console owns its own scroll (the log is the page), so it
 * only borrows the titlebar clearance and header — not the nested scroller
 * that would clip the toolbar and fight the list.
 */
const Console = () => (
  <div className="flex min-h-0 flex-1 flex-col pt-8">
    <Header
      isMainTitle
      showBorder
      title="Console"
      description="What actually happened, scrubbed of credentials — exportable for bug reports."
    />
    <DebugConsole
      chrome="page"
      className="mt-4 min-h-0 flex-1 rounded-xl border border-border"
    />
  </div>
);

export default Console;
