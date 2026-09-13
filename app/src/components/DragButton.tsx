import { GripVerticalIcon } from "lucide-react";
import { Button } from "@/components";

/**
 * The overlay's drag handle. Upstream gated dragging behind an active
 * license and used this button to open a purchase popover; Kaleo has no
 * licenses, so the handle is always just a handle.
 */
export const DragButton = () => {
  return (
    <Button
      variant="ghost"
      size="icon"
      className={`-ml-[2px] w-fit`}
      data-tauri-drag-region
    >
      <GripVerticalIcon className="h-4 w-4" data-tauri-drag-region />
    </Button>
  );
};
