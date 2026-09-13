import { useState } from "react";
import { SkinPreviewPicker, StepFrame, StripPreview, ThemeCardPicker } from "@/components/setup";
import { getSkin, type SkinId } from "@/lib/overlay-skin";

export const APPEARANCE_TITLE = "Choose your look";

/**
 * Theme first, then the strip's shape, with a strip preview at the top so
 * "strip" means something before the cards ask about it. Both pickers
 * apply live: the theme through `useTheme`, the skin through localStorage,
 * which the overlay webview already follows.
 */
export const AppearanceStep = () => {
  const [skin, setSkin] = useState<SkinId>(() => getSkin());
  return (
    <StepFrame stepId="appearance" title={APPEARANCE_TITLE} subtitle="You can change this later in Settings.">
      <div className="flex flex-col items-center gap-6">
        <StripPreview skin={skin} size="lg" />
        <ThemeCardPicker />
        <SkinPreviewPicker className="w-full" onChange={setSkin} />
      </div>
    </StepFrame>
  );
};
