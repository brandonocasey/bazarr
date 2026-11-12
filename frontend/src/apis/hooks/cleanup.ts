import { useEffect } from "react";
import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import cleanup, {
  CleanupMedia,
  cleanupMediaKey,
  CleanupOperation,
} from "@/apis/raw/cleanup";

const operationKey = (media: CleanupMedia, id: string) =>
  [QueryKeys.Subtitles, "cleanup", cleanupMediaKey(media), id] as const;

export const useCleanupHistories = (media: CleanupMedia[]) =>
  useQueries({
    queries: media.map((item) => ({
      queryKey: [
        QueryKeys.Subtitles,
        "cleanup",
        cleanupMediaKey(item),
        "history",
      ],
      queryFn: () => cleanup.list(item),
      placeholderData: undefined,
    })),
  });

export const useCleanupOperations = (
  media: CleanupMedia[],
  ids: Record<string, string>,
) => {
  const client = useQueryClient();
  const queries = useQueries({
    queries: media.map((item) => ({
      queryKey: operationKey(item, ids[cleanupMediaKey(item)] ?? ""),
      queryFn: async () => {
        const operation = await cleanup.get(ids[cleanupMediaKey(item)]);
        if (cleanupMediaKey(operation) !== cleanupMediaKey(item)) {
          throw new Error(
            "Cleanup operation belongs to different media. Create a new preview.",
          );
        }
        return operation;
      },
      enabled: Boolean(ids[cleanupMediaKey(item)]),
      placeholderData: undefined,
      refetchOnWindowFocus: true,
      refetchInterval: (query: { state: { data?: CleanupOperation } }) =>
        query.state.data &&
        ["queued", "analyzing", "applying", "restoring"].includes(
          query.state.data.status,
        )
          ? 1000
          : false,
    })),
  });
  const finished = queries
    .map((query) => query.data)
    .filter(
      (operation): operation is CleanupOperation =>
        operation !== undefined &&
        ["complete", "restored", "failed"].includes(operation.status),
    )
    .map(
      (operation) =>
        `${cleanupMediaKey(operation)}:${operation.id}:${operation.status}`,
    )
    .join(",");

  useEffect(() => {
    if (finished) {
      void client.invalidateQueries({ queryKey: [QueryKeys.Movies] });
      void client.invalidateQueries({ queryKey: [QueryKeys.Episodes] });
      void client.invalidateQueries({ queryKey: [QueryKeys.Series] });
      void client.invalidateQueries({ queryKey: [QueryKeys.History] });
      void client.invalidateQueries({
        queryKey: [QueryKeys.Subtitles, "cleanup"],
        predicate: (query) => query.queryKey.at(-1) === "history",
      });
    }
  }, [client, finished]);
  return queries;
};

export const useCleanupActions = () => {
  const client = useQueryClient();
  const remember = (operation: CleanupOperation) => {
    client.setQueryData(operationKey(operation, operation.id), operation);
    void client.invalidateQueries({
      queryKey: [
        QueryKeys.Subtitles,
        "cleanup",
        cleanupMediaKey(operation),
        "history",
      ],
    });
  };
  return {
    preview: useMutation({
      mutationFn: ({ media, audio }: { media: CleanupMedia; audio?: number }) =>
        cleanup.preview(media, audio),
      onSuccess: remember,
    }),
    apply: useMutation({
      mutationFn: ({
        operation,
        keepers,
      }: {
        operation: CleanupOperation;
        keepers: Record<string, string>;
      }) => cleanup.apply(operation.id, operation.preview_token, keepers),
      onSuccess: remember,
    }),
    restore: useMutation({
      mutationFn: (operation: CleanupOperation) =>
        cleanup.restore(operation.id, operation.preview_token),
      onSuccess: remember,
    }),
  };
};
