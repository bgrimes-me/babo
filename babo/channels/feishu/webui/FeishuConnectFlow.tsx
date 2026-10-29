import { ChannelQrConnectFlow } from "@/components/settings/channels/ChannelQrConnectFlow";
import type { BaboFeaturesPayload } from "@/lib/types";

import { FeishuConnectFlowView, useFeishuConnectLabels } from "./FeishuConnectFlowView";

export function FeishuConnectFlow({
  instanceId = "default",
  mode = "replace",
  idleLabel,
  connectRequestId,
  onFeaturesUpdate,
}: {
  token: string;
  instanceId?: string;
  mode?: "replace" | "create";
  idleLabel?: string;
  connectRequestId?: number;
  onFeaturesUpdate: (payload: BaboFeaturesPayload) => void;
}) {
  const labels = useFeishuConnectLabels();
  return (
    <ChannelQrConnectFlow
      channelName="feishu"
      startParams={{ domain: "feishu", instance_id: instanceId, mode }}
      idleLabel={idleLabel}
      connectRequestId={connectRequestId}
      onFeaturesUpdate={onFeaturesUpdate}
      labels={labels}
      renderView={(props) => <FeishuConnectFlowView {...props} />}
    />
  );
}
