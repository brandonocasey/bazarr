import { PropsWithChildren } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw/http";
import { describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import {
  CleanupMedia,
  cleanupMediaKey,
  CleanupOperation,
} from "@/apis/raw/cleanup";
import server from "@/tests/mocks/node";
import { useCleanupActions, useCleanupOperations } from "./cleanup";

const wrapper = ({ children }: PropsWithChildren) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
);
const movie: CleanupMedia = {
  media_type: "movie",
  media_id: 7,
  title: "Movie",
};
const episode: CleanupMedia = {
  ...movie,
  media_type: "series",
  title: "Episode",
};
const operation = (
  media: CleanupMedia,
  id = cleanupMediaKey(media),
): CleanupOperation => ({
  ...media,
  id,
  status: "queued",
  preview_token: "token",
  analysis: null,
  groups: [],
  files: [],
  required_space_bytes: 0,
  backup_path: null,
  result: null,
  error: null,
  created_at: "2026-11-12T12:00:00Z",
});
const statusKey = [QueryKeys.Subtitles, "cleanup", "status"];

describe("cleanup batch polling", () => {
  it.each([300, 3000])(
    "sends one request per tick for %i operations",
    async (count) => {
      const media: CleanupMedia[] = Array.from(
        { length: count },
        (_, mediaId) => ({ ...movie, media_id: mediaId }),
      );
      const ids = Object.fromEntries(
        media.map((item) => [cleanupMediaKey(item), cleanupMediaKey(item)]),
      );
      const operations = new Map(
        media.map((item) => [cleanupMediaKey(item), operation(item)]),
      );
      const batches: string[][] = [];
      const singles = vi.fn();
      server.use(
        http.get("/api/subtitles/cleanup/:id", ({ params }) => {
          singles();
          return HttpResponse.json(operations.get(String(params.id)));
        }),
        http.post("/api/subtitles/cleanup/status", async ({ request }) => {
          const { operation_ids } = (await request.json()) as {
            operation_ids: string[];
          };
          batches.push(operation_ids);
          return HttpResponse.json({
            data: operation_ids.map((id) => ({
              id,
              operation: {
                ...operations.get(id),
                preview_token: `tick-${batches.length}`,
              },
              error: null,
            })),
          });
        }),
      );
      const refresh = vi.fn();
      const { result, rerender, unmount } = renderHook(
        () =>
          useCleanupOperations(
            media.map((item) => ({ ...item })),
            { ...ids },
            refresh,
          ),
        { wrapper },
      );
      try {
        await waitFor(() => expect(batches).toHaveLength(1));
        await waitFor(() =>
          expect(
            result.current.every(
              (row) => row.data?.status === "queued" && !row.isFetching,
            ),
          ).toBe(true),
        );
        rerender();
        expect(batches).toHaveLength(1);
        for (const tick of [1, 2]) {
          await waitFor(() => expect(batches).toHaveLength(tick + 1), {
            timeout: 2500,
          });
          await waitFor(() =>
            expect(result.current.every((row) => !row.isFetching)).toBe(true),
          );
          expect(batches[tick]).toEqual([...operations.keys()]);
          expect(
            result.current.every(
              (row) => row.data?.preview_token === `tick-${tick + 1}`,
            ),
          ).toBe(true);
        }
        expect(result.current).toHaveLength(count);
        expect(singles).not.toHaveBeenCalled();
        expect(refresh).toHaveBeenCalledTimes(3);
      } finally {
        unmount();
      }
    },
    20000,
  );

  it("does not request status for empty media or missing IDs", async () => {
    const requests = vi.fn();
    const refresh = vi.fn();
    server.use(
      http.post("/api/subtitles/cleanup/status", () => {
        requests();
        return HttpResponse.json({ data: [] });
      }),
    );
    const { result, rerender } = renderHook(
      ({
        media,
        ids,
      }: {
        media: CleanupMedia[];
        ids: Record<string, string>;
      }) => useCleanupOperations(media, ids, refresh),
      {
        wrapper,
        initialProps: {
          media: [] as CleanupMedia[],
          ids: {} as Record<string, string>,
        },
      },
    );
    expect(result.current).toEqual([]);
    rerender({ media: [movie], ids: {} });
    expect(result.current[0].data).toBeUndefined();
    expect(result.current[0].isFetching).toBe(false);
    expect(requests).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("keeps independent row errors and continues polling when another row finishes", async () => {
    const tick = { value: 0 };
    const requests = vi.fn();
    const media = [movie, episode, { ...movie, media_id: 8 }];
    const ids = Object.fromEntries(
      media.map((item) => [cleanupMediaKey(item), cleanupMediaKey(item)]),
    );
    server.use(
      http.post("/api/subtitles/cleanup/status", () => {
        requests();
        return HttpResponse.json({
          data: media.map((item, index) => ({
            id: cleanupMediaKey(item),
            operation:
              tick.value && index === 0
                ? null
                : {
                    ...operation(item),
                    status: tick.value && index === 1 ? "complete" : "queued",
                  },
            error: tick.value && index === 0 ? "Operation disappeared" : null,
          })),
        });
      }),
    );
    const { result, unmount } = renderHook(
      () => useCleanupOperations(media, ids, () => undefined),
      { wrapper },
    );
    try {
      await waitFor(() =>
        expect(
          result.current.every(
            (row) => row.data?.status === "queued" && !row.isFetching,
          ),
        ).toBe(true),
      );
      tick.value++;
      await waitFor(
        () =>
          expect(result.current[0].error?.message).toBe(
            "Operation disappeared",
          ),
        { timeout: 2500 },
      );
      expect(result.current[0].data?.id).toBe("movie:7");
      expect(result.current[1].data?.status).toBe("complete");
      expect(result.current[1].isError).toBe(false);
      expect(result.current[2].data?.status).toBe("queued");
      tick.value++;
      await waitFor(() => expect(requests).toHaveBeenCalledTimes(3), {
        timeout: 2500,
      });
      await waitFor(() => expect(result.current[2].isFetching).toBe(false));
    } finally {
      unmount();
    }
  });

  it("rejects wrong media and wrong operation IDs without exposing their data", async () => {
    server.use(
      http.post("/api/subtitles/cleanup/status", () =>
        HttpResponse.json({
          data: [
            {
              id: "movie-op",
              operation: operation(episode, "movie-op"),
              error: null,
            },
            {
              id: "series-op",
              operation: operation(episode, "other-op"),
              error: null,
            },
          ],
        }),
      ),
    );
    const { result } = renderHook(
      () =>
        useCleanupOperations(
          [movie, episode],
          { "movie:7": "movie-op", "series:7": "series-op" },
          () => undefined,
        ),
      { wrapper },
    );
    await waitFor(() =>
      expect(result.current.every((row) => row.isError)).toBe(true),
    );
    expect(result.current.every((row) => row.data === undefined)).toBe(true);
    expect(result.current[0].error?.message).toMatch(/different media/);
    expect(result.current[1].error?.message).toMatch(/different operation/);
  });

  it("does not apply a late response to swapped or removed IDs", async () => {
    const { promise: pending, resolve: finish } = Promise.withResolvers<void>();
    const requests: string[][] = [];
    server.use(
      http.post("/api/subtitles/cleanup/status", async ({ request }) => {
        const { operation_ids } = (await request.json()) as {
          operation_ids: string[];
        };
        requests.push(operation_ids);
        if (operation_ids.includes("old")) await pending;
        return HttpResponse.json({
          data: operation_ids.map((id) => ({
            id,
            operation: operation(movie, id),
            error: null,
          })),
        });
      }),
    );
    const { result, rerender } = renderHook(
      ({ ids }: { ids: Record<string, string> }) =>
        useCleanupOperations([movie], ids, () => undefined),
      {
        wrapper,
        initialProps: { ids: { "movie:7": "old" } as Record<string, string> },
      },
    );
    await waitFor(() => expect(requests).toHaveLength(1));
    rerender({ ids: { "movie:7": "new" } });
    await waitFor(() => expect(result.current[0].data?.id).toBe("new"));
    await act(async () => {
      finish();
      await pending;
    });
    expect(result.current[0].data?.id).toBe("new");
    rerender({ ids: {} });
    expect(result.current[0].data).toBeUndefined();
    expect(result.current[0].isError).toBe(false);
  });

  it("keeps refreshed row data when another ID is added or removed", async () => {
    const preview = operation(movie, "first");
    queryClient.setQueryData(
      [QueryKeys.Subtitles, "cleanup", "movie:7", "first"],
      preview,
    );
    server.use(
      http.post("/api/subtitles/cleanup/status", async ({ request }) => {
        const { operation_ids } = (await request.json()) as {
          operation_ids: string[];
        };
        return HttpResponse.json({
          data: operation_ids.map((id) => ({
            id,
            operation: {
              ...operation(id === "first" ? movie : episode, id),
              status: "ready",
              preview_token: "refreshed",
            },
            error: null,
          })),
        });
      }),
    );
    const initialProps: { ids: Record<string, string> } = {
      ids: { "movie:7": "first" },
    };
    const { result, rerender } = renderHook(
      ({ ids }: { ids: Record<string, string> }) =>
        useCleanupOperations([movie, episode], ids, () => undefined),
      { wrapper, initialProps },
    );
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: statusKey });
    });
    await waitFor(() =>
      expect(result.current[0].data?.preview_token).toBe("refreshed"),
    );
    rerender({ ids: { "movie:7": "first", "series:7": "second" } });
    expect(result.current[0].data?.preview_token).toBe("refreshed");
    await waitFor(() => expect(result.current[1].data?.id).toBe("second"));
    rerender(initialProps);
    expect(result.current[0].data?.preview_token).toBe("refreshed");
    expect(result.current[1].data).toBeUndefined();
  });

  it("preserves an apply result when an older same-ID refresh finishes late", async () => {
    const ready = { ...operation(movie, "current"), status: "ready" as const };
    queryClient.setQueryData(
      [QueryKeys.Subtitles, "cleanup", "movie:7", "current"],
      ready,
    );
    const { promise: pending, resolve: finish } = Promise.withResolvers<void>();
    const refreshStarted = vi.fn();
    server.use(
      http.post("/api/subtitles/cleanup/status", async () => {
        refreshStarted();
        await pending;
        return HttpResponse.json({
          data: [{ id: ready.id, operation: ready, error: null }],
        });
      }),
      http.post("/api/subtitles/cleanup/current/apply", () =>
        HttpResponse.json({ ...ready, status: "applying" }, { status: 202 }),
      ),
    );
    const { result } = renderHook(
      () => ({
        rows: useCleanupOperations(
          [movie],
          { "movie:7": "current" },
          () => undefined,
        ),
        actions: useCleanupActions(),
      }),
      { wrapper },
    );
    const refresh = { current: Promise.resolve() };
    await act(async () => {
      refresh.current = queryClient.invalidateQueries({ queryKey: statusKey });
    });
    await waitFor(() => expect(refreshStarted).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.actions.apply.mutateAsync({
        operation: ready,
        keepers: {},
      });
    });
    await waitFor(() =>
      expect(result.current.rows[0].data?.status).toBe("applying"),
    );
    await act(async () => {
      finish();
      await pending;
      await refresh.current;
    });
    expect(
      queryClient.getQueryData<CleanupOperation>([
        QueryKeys.Subtitles,
        "cleanup",
        "movie:7",
        "current",
      ])?.status,
    ).toBe("applying");
    expect(result.current.rows[0].data?.status).toBe("applying");
  });

  it.each(["transport", "malformed", "missing"])(
    "exposes %s status failures for all affected rows",
    async (failure) => {
      server.use(
        http.post("/api/subtitles/cleanup/status", () => {
          if (failure === "transport")
            return HttpResponse.json(
              { error: "Status unavailable" },
              { status: 503 },
            );
          return HttpResponse.json(
            failure === "malformed" ? { data: null } : { data: [] },
          );
        }),
      );
      const { result } = renderHook(
        () =>
          useCleanupOperations(
            [movie, episode],
            { "movie:7": "movie-op", "series:7": "series-op" },
            () => undefined,
          ),
        { wrapper },
      );
      await waitFor(() =>
        expect(
          result.current.every((row) => row.isError && !row.isFetching),
        ).toBe(true),
      );
      const message = {
        transport: "Status unavailable",
        malformed: "Invalid cleanup status response. Retry the operation.",
        missing: "Cleanup operation status is missing. Retry the operation.",
      }[failure];
      expect(result.current.map((row) => row.error?.message)).toEqual([
        message,
        message,
      ]);
      await act(async () => {
        await queryClient.invalidateQueries({ queryKey: statusKey });
      });
    },
  );
});
