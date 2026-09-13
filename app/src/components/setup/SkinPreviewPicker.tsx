import { useRef, useState } from "react";
import { Badge } from "@/components/ui";
import { SKINS, type SkinId, getSkin, setSkin } from "@/lib/overlay-skin";
import { StripPreview } from "./StripPreview";
import { rovingKeyDown } from "./roving";

interface SkinPreviewPickerProps {
  className?: string;
  /** Called after the choice is stored; the settings pane uses it for the Ada switch. */
  onChange?: (id: SkinId) => void;
}

/**
 * Four live strip previews, one radiogroup.
 *
 * An unbuilt skin is still selectable — the preference stores and survives to
 * the build that draws it — but it says "Not built yet" and dims, because
 * offering four options of which two do nothing is the thing this catalogue
 * refuses to do. The `summary` under each name is the catalogue's own.
 */
export const SkinPreviewPicker = ({ className, onChange }: SkinPreviewPickerProps) => {
  const [skin, setSkinState] = useState<SkinId>(() => getSkin());
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const ids = SKINS.map((s) => s.id);

  const choose = (id: SkinId) => {
    setSkin(id);
    setSkinState(id);
    onChange?.(id);
  };
  const onKeyDown = rovingKeyDown(ids, skin, choose, (i) => refs.current[i]?.focus());

  return (
    <div
      role="radiogroup"
      aria-label="Strip style"
      className={`grid grid-cols-2 gap-3 ${className ?? ""}`}
      onKeyDown={onKeyDown}
      data-testid="skin-picker"
    >
      {SKINS.map((option, i) => {
        const selected = option.id === skin;
        return (
          <button
            key={option.id}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => choose(option.id)}
            data-testid="skin-card"
            data-skin={option.id}
            data-built={option.built ? "true" : "false"}
            className={`flex cursor-pointer flex-col items-start gap-2 rounded-lg border p-3 text-left outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring/60 ${
              selected ? "border-foreground/60 bg-accent/40" : "border-border hover:bg-accent/20"
            } ${option.built ? "" : "opacity-60"}`}
          >
            <StripPreview skin={option.id} />
            <span className="flex w-full items-center justify-between gap-2">
              <span className={`text-[13px] ${selected ? "font-medium" : ""}`}>{option.name}</span>
              {!option.built ? (
                <Badge
                  variant="outline"
                  className="text-[10px] text-muted-foreground"
                  data-testid="skin-unbuilt"
                >
                  Not built yet
                </Badge>
              ) : null}
            </span>
            <span className="text-[11px] leading-snug text-muted-foreground">{option.summary}</span>
          </button>
        );
      })}
    </div>
  );
};
