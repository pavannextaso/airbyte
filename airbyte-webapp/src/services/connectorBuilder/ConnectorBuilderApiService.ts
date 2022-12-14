import { useQuery } from "react-query";

import { useConfig } from "config";
import { ConnectorBuilderRequestService } from "core/domain/connectorBuilder/ConnectorBuilderRequestService";
import {
  StreamReadRequestBody,
  StreamReadRequestBodyConfig,
  StreamReadRequestBodyManifest,
  StreamsListRequestBody,
} from "core/request/ConnectorBuilderClient";
import { useSuspenseQuery } from "services/connector/useSuspenseQuery";
import { useDefaultRequestMiddlewares } from "services/useDefaultRequestMiddlewares";
import { useInitService } from "services/useInitService";

const connectorBuilderKeys = {
  all: ["connectorBuilder"] as const,
  read: (streamName: string, manifest: StreamReadRequestBodyManifest, config: StreamReadRequestBodyConfig) =>
    [...connectorBuilderKeys.all, "read", { streamName, manifest, config }] as const,
  list: (manifest: StreamReadRequestBodyManifest) => [...connectorBuilderKeys.all, "list", { manifest }] as const,
};

function useConnectorBuilderService() {
  const config = useConfig();
  const middlewares = useDefaultRequestMiddlewares();
  return useInitService(
    () => new ConnectorBuilderRequestService(config.connectorBuilderApiUrl, middlewares),
    [config.connectorBuilderApiUrl, middlewares]
  );
}

export const useReadStream = (params: StreamReadRequestBody) => {
  const service = useConnectorBuilderService();

  return useQuery(
    connectorBuilderKeys.read(params.stream, params.manifest, params.config),
    () => service.readStream(params),
    { refetchOnWindowFocus: false, enabled: false }
  );
};

export const useListStreams = (params: StreamsListRequestBody) => {
  const service = useConnectorBuilderService();

  return useSuspenseQuery(connectorBuilderKeys.list(params.manifest), () => service.listStreams(params));
};
