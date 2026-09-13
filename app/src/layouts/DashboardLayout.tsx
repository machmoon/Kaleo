import { Sidebar } from "@/components";
import { Tour } from "@/components/Tour";
import { Outlet } from "react-router-dom";
import { ErrorBoundary } from "react-error-boundary";
import { ErrorLayout } from "./ErrorLayout";

export const DashboardLayout = () => {
  return (
    <ErrorBoundary
      fallbackRender={() => {
        return <ErrorLayout />;
      }}
      resetKeys={["dashboard-error"]}
      onReset={() => {
        console.log("Reset");
      }}
    >
      <div className="relative flex h-screen w-screen overflow-hidden bg-background">
        {/* Draggable region */}
        <div
          className="absolute left-0 right-0 top-0 z-50 h-10 select-none"
          data-tauri-drag-region={true}
        />

        {/* Sidebar */}
        <Sidebar />
        {/* Main Content — min-w-0 so a wide child (console toolbar, long
            monospace lines) shrinks and wraps instead of clipping the pane. */}
        <main className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden px-8">
          <Outlet />
        </main>
        {/* The Tips cards of the tour; dashboard-only, draws nothing until started. */}
        <Tour />
      </div>
    </ErrorBoundary>
  );
};
