import type { KeyboardEvent } from "react";

/**
 * Roving tabindex for a `role="radiogroup"` of buttons.
 *
 * One stop in the tab order (the selected item), arrows move the selection,
 * Space/Enter re-affirm it. `onKeyDown` is meant for the group element so the
 * items themselves stay plain buttons.
 */
export function rovingKeyDown<T>(
  items: readonly T[],
  selected: T,
  onSelect: (item: T) => void,
  focusItem: (index: number) => void,
) {
  return (event: KeyboardEvent<HTMLElement>) => {
    const index = Math.max(0, items.indexOf(selected));
    let next = index;
    switch (event.key) {
      case "ArrowRight":
      case "ArrowDown":
        next = (index + 1) % items.length;
        break;
      case "ArrowLeft":
      case "ArrowUp":
        next = (index - 1 + items.length) % items.length;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = items.length - 1;
        break;
      case " ":
      case "Enter":
        event.preventDefault();
        onSelect(items[index]);
        return;
      default:
        return;
    }
    event.preventDefault();
    onSelect(items[next]);
    focusItem(next);
  };
}
