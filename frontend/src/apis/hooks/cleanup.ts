import { useEffect, useRef } from "react";
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
  onRefresh: () => void,
) => {
  const client = useQueryClient();
  const queries = useQueries({
    queries: media.map((item) => ({
      queryKey: operationKey(item, ids[cleanupMediaKey(item)] ?? ""),
      queryFn: async () => {
        onRefresh();
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
  const finished = useRef(new Set<string>());
  useEffect(() => {
    const current = new Set<string>();
    const changed: CleanupOperation[] = [];
    for (const query of queries) {
      const operation = query.data;
      if (
        !operation ||
        !["complete", "restored", "failed"].includes(operation.status)
      )
        continue;
      const key = JSON.stringify([
        cleanupMediaKey(operation),
        operation.id,
        operation.status,
      ]);
      current.add(key);
      if (!finished.current.has(key)) changed.push(operation);
    }
    finished.current = current;
    if (!changed.length) return;
    void client.invalidateQueries({ queryKey: [QueryKeys.Movies] });
    void client.invalidateQueries({ queryKey: [QueryKeys.Episodes] });
    void client.invalidateQueries({ queryKey: [QueryKeys.Series] });
    void client.invalidateQueries({ queryKey: [QueryKeys.History] });
    for (const operation of changed) {
      void client.invalidateQueries({
        queryKey: [
          QueryKeys.Subtitles,
          "cleanup",
          cleanupMediaKey(operation),
          "history",
        ],
        exact: true,
      });
    }
  }, [client, queries]);
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
