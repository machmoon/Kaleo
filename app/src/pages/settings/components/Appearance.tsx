import { useTheme } from "@/contexts";
import { Header, Slider } from "@/components";
import { ThemeCardPicker } from "@/components/setup";

/**
 * Apple's Appearance picker in place of the old dropdown, with the
 * transparency slider it replaces kept underneath. Same `useTheme`, same
 * localStorage key, so the other webview follows as before.
 */
export const Appearance = () => {
  const { transparency, onSetTransparency } = useTheme();

  return (
    <div id="theme" className="relative space-y-4" data-testid="appearance-settings">
      <Header title="Appearance" description="Auto follows macOS. Light and Dark stay put." isMainTitle />

      <ThemeCardPicker className="justify-start" />

      <div className="space-y-2">
        <Header title="Window transparency" description="How much of what is behind the dashboard shows through" />
        <div className="space-y-3">
          <div className="mt-4 flex items-center gap-4">
            <Slider
              value={[transparency]}
              onValueChange={(value: number[]) => onSetTransparency(value[0])}
              min={0}
              max={100}
              step={1}
              className="flex-1"
              aria-label="Window transparency"
            />
          </div>
          <p className="text-xs text-muted-foreground/70">Changes apply immediately.</p>
        </div>
      </div>
    </div>
  );
};
