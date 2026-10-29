import type { ComponentType } from "react";

import type { ChannelPresentation } from "@/channel-plugins/catalog";
import type {
  BaboFeatureInfo,
  BaboFeaturesPayload,
} from "@/lib/types";

export type ChannelFeatureActionOptions = {
  confirmed?: boolean;
  installOnly?: boolean;
};

export type ChannelFeatureAction = (
  action: "enable" | "disable",
  name: string,
  options?: ChannelFeatureActionOptions,
) => void;

export type ChannelPluginPanelProps = {
  connectRequestId?: number;
  token: string;
  feature: BaboFeatureInfo;
  actionKey: string | null;
  showBrandLogos: boolean;
  onAction: ChannelFeatureAction;
  onFeaturesUpdate: (payload: BaboFeaturesPayload) => void;
  onConfigureMcp?: (name: string) => void;
  onBeforeCloseChange?: (handler: (() => Promise<boolean>) | null) => void;
};

export type ChannelPluginConnectFlowProps = {
  token: string;
  feature: BaboFeatureInfo;
  idleLabel?: string;
  connectRequestId?: number;
  onOpenAdvanced?: () => void;
  onFeaturesUpdate: (payload: BaboFeaturesPayload) => void;
};

export type ChannelPluginHelpProps = {
  feature: BaboFeatureInfo;
};

export type ChannelUiContribution = {
  presentation: ChannelPresentation;
  aliases?: Record<string, Partial<ChannelPresentation>>;
  Panel?: ComponentType<ChannelPluginPanelProps>;
  HelpContent?: ComponentType<ChannelPluginHelpProps>;
  ConnectFlow?: ComponentType<ChannelPluginConnectFlowProps>;
  canConnectBeforeConfigured?: boolean;
};

export type RegisteredChannelUiContribution = {
  channel: string;
  webui: string;
  contribution: ChannelUiContribution;
};
