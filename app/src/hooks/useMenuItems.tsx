import {
  Settings,
  SquareSlashIcon,
  PowerIcon,
  CircuitBoardIcon,
  CableIcon,
  PlugZapIcon,
  TerminalIcon,
} from "lucide-react";
import { invoke } from "@tauri-apps/api/core";

/**
 * The dashboard window's navigation. Only the Kaleo surfaces remain: the
 * Pluely chat verticals (chats, system prompts, responses, screenshot, audio,
 * dev space, the old dashboard) were removed with the chat product, along
 * with the license-gated support link and upstream's promotional footer.
 */
export const useMenuItems = () => {
  const menu: {
    icon: React.ElementType;
    label: string;
    href: string;
    count?: number;
  }[] = [
    {
      icon: CircuitBoardIcon,
      label: "Workbench",
      href: "/workbench",
    },
    {
      icon: CableIcon,
      label: "Engine",
      href: "/engine",
    },
    {
      icon: PlugZapIcon,
      label: "Integrations",
      href: "/integrations",
    },
    {
      icon: TerminalIcon,
      label: "Console",
      href: "/console",
    },
    {
      icon: Settings,
      label: "App Settings",
      href: "/settings",
    },
    {
      icon: SquareSlashIcon,
      label: "Shortcuts",
      href: "/shortcuts",
    },
  ];

  const footerItems = [
    {
      icon: PowerIcon,
      label: "Quit Hardy",
      action: async () => {
        await invoke("exit_app");
      },
    },
  ];

  const footerLinks: {
    title: string;
    icon: React.ElementType;
    link: string;
  }[] = [];

  return {
    menu,
    footerItems,
    footerLinks,
  };
};
