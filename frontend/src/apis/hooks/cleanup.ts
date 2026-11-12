import { useEffect, useRef } from "react";
import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import cleanup, {
  CleanupMedia,
  cleanupMediaKey,
  CleanupOperation,
  CleanupOperationStatus,
} from "@/apis/raw/cleanup";

const operationKey = (media: CleanupMedia, id: string) =>
  [QueryKeys.Subtitles, "cleanup", cleanupMediaKey(media), id] as const;

const operationOwnershipError = (
  operation: CleanupOperation | undefined,
  media: CleanupMedia,
  id: string,
) => {
  if (!operation) return null;
  if (operation.id !== id)
    return new Error(
      "Server returned a different operation. Create a new preview.",
    );
  if (cleanupMediaKey(operation) !== cleanupMediaKey(media))
    return new Error(
      "Cleanup operation belongs to different media. Create a new preview.",
    );
  return null;
};

const statusKey = [QueryKeys.Subtitles, "cleanup", "status"] as const;

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
  const entries = media.map((item) => [
    cleanupMediaKey(item),
    ids[cleanupMediaKey(item)] ?? "",
  ]);
  const operationIds = [
    ...new Set(entries.map(([, id]) => id).filter(Boolean)),
  ];
  const queryKey = [...statusKey, entries];
  const batch = useQuery({
    queryKey,
    enabled: operationIds.length > 0,
    placeholderData: undefined,
    gcTime: 0,
    initialData: () => {
      const operations = media.flatMap((item) => {
        const id = ids[cleanupMediaKey(item)];
        if (!id) return [];
        const operation = client.getQueryData<CleanupOperation>(
          operationKey(item, id),
        );
        return operation ? [{ id, operation, error: null }] : [];
      });
      return operations.length === operationIds.length &&
        operationIds.length > 0
        ? operations
        : undefined;
    },
    queryFn: async ({ signal }) => {
      onRefresh();
      const statuses = await cleanup.status(operationIds, signal);
      const owners = new Map(entries.map(([key, id]) => [id, key]));
      for (const { id, operation, error } of statuses) {
        if (
          !error &&
          operation &&
          operation.id === id &&
          cleanupMediaKey(operation) === owners.get(id)
        )
          client.setQueryData(operationKey(operation, id), operation);
      }
      return statuses;
    },
    refetchOnWindowFocus: true,
    refetchInterval: (query) =>
      query.state.data?.some(
        (entry) =>
          !entry.error &&
          entry.operation &&
          ["queued", "analyzing", "applying", "restoring"].includes(
            entry.operation.status,
          ),
      )
        ? 1000
        : false,
  });
  const statuses = new Map(batch.data?.map((entry) => [entry.id, entry]));
  const queries = media.map((item) => {
    const id = ids[cleanupMediaKey(item)];
    if (!id)
      return {
        data: undefined,
        error: null,
        isError: false as const,
        isFetching: false,
        isPending: true,
        refetch: batch.refetch,
      };
    const status = statuses.get(id);
    const candidate =
      status?.operation ??
      client.getQueryData<CleanupOperation>(operationKey(item, id));
    const ownershipError = operationOwnershipError(candidate, item, id);
    const data = ownershipError ? undefined : candidate;
    const statusError =
      status?.error ??
      (!status && batch.isSuccess
        ? "Cleanup operation status is missing. Retry the operation."
        : null);
    const error =
      batch.error ??
      ownershipError ??
      (statusError ? new Error(statusError) : null);
    const state = {
      data: data ?? undefined,
      isFetching: batch.isFetching,
      isPending: batch.isPending && !data,
      refetch: batch.refetch,
    };
    return error
      ? { ...state, error, isError: true as const }
      : { ...state, error: null, isError: false as const };
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
    const changedMedia = new Set(changed.map(cleanupMediaKey));
    void client.invalidateQueries({
      queryKey: [QueryKeys.Subtitles, "cleanup"],
      predicate: (query) =>
        query.queryKey.length === 4 &&
        query.queryKey[3] === "history" &&
        typeof query.queryKey[2] === "string" &&
        changedMedia.has(query.queryKey[2]),
    });
  }, [client, queries]);
  return queries;
};

export const useCleanupActions = () => {
  const client = useQueryClient();
  const remember = (operation: CleanupOperation) => {
    void client.cancelQueries({ queryKey: statusKey });
    client.setQueryData(operationKey(operation, operation.id), operation);
    client.setQueriesData<CleanupOperationStatus[]>(
      { queryKey: statusKey },
      (statuses) =>
        statuses?.map((entry) =>
          entry.id === operation.id
            ? { id: operation.id, operation, error: null }
            : entry,
        ),
    );
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
