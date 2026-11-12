import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw/http";
import { describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { CleanupMedia, CleanupOperation } from "@/apis/raw/cleanup";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { SubtitleCleanupView } from "./SubtitleCleanupModal";

const movie: CleanupMedia = {
  media_type: "movie",
  media_id: 7,
  title: "Film #2 日本語",
};
const episode: CleanupMedia = {
  media_type: "series",
  media_id: 7,
  title: "Episode 7",
};
const operation = (media = movie): CleanupOperation => ({
  ...media,
  id: `${media.media_type}-operation`,
  status: "ready",
  preview_token: `${media.media_type}-fingerprint`,
  analysis: {
    video_path: "/media/Film #2 日本語.mkv",
    duration: 120,
    audio_stream_index: 1,
    audio_streams: [
      { index: 1, language: "eng", title: "Dialogue", default: true },
      { index: 4, language: "jpn", title: "Original", default: false },
    ],
    tracks: [
      {
        key: "normal",
        companion_paths: [],
        source: "external",
        path: "/media/Film #2 日本語.en.srt",
        stream_index: 0,
        language: "eng",
        forced: false,
        hearing_impaired: false,
        codec: "srt",
        score: 0.8,
        download_score: 92,
        error: null,
      },
      {
        key: "hi",
        companion_paths: [],
        source: "embedded",
        path: "/media/Film #2 日本語.mkv",
        stream_index: 2,
        language: "eng",
        forced: false,
        hearing_impaired: true,
        codec: "pgs",
        score: 0.4,
        download_score: null,
        error: null,
      },
      {
        key: "forced",
        companion_paths: [],
        source: "embedded",
        path: "/media/Film #2 日本語.mkv",
        stream_index: 3,
        language: "eng",
        forced: true,
        hearing_impaired: false,
        codec: "ass",
        score: null,
        download_score: null,
        error: "constant signal",
      },
      {
        key: "unknown-a",
        companion_paths: ["/media/a.idx"],
        source: "external",
        path: "/media/a.sub",
        stream_index: 0,
        language: null,
        forced: false,
        hearing_impaired: false,
        codec: "vobsub",
        score: null,
        download_score: null,
        error: "no speech variance",
      },
      {
        key: "unknown-b",
        companion_paths: ["/media/b.idx"],
        source: "external",
        path: "/media/b.sub",
        stream_index: 0,
        language: null,
        forced: false,
        hearing_impaired: true,
        codec: "vobsub",
        score: null,
        download_score: null,
        error: "no subtitle variance",
      },
    ],
    algorithm: { correlation: "Pearson", parser: "test-1" },
  },
  groups: [
    {
      key: "eng",
      language: "eng",
      forced: false,
      track_keys: ["normal", "hi"],
      recommended_keeper: "normal",
      requires_selection: false,
    },
    {
      key: "eng-forced",
      language: "eng",
      forced: true,
      track_keys: ["forced"],
      recommended_keeper: "forced",
      requires_selection: false,
    },
    {
      key: "unknown",
      language: null,
      forced: false,
      track_keys: ["unknown-a", "unknown-b"],
      recommended_keeper: null,
      requires_selection: true,
    },
  ],
  files: [
    { path: "/media/Film #2 日本語.mkv", size: 1000, sha256: "abc" },
    { path: "/media/b.idx", size: 24, sha256: "def" },
  ],
  required_space_bytes: 2048,
  backup_path: null,
  result: null,
  error: null,
  created_at: "2026-11-12T12:00:00Z",
});
const register = (preview = operation()) => {
  server.use(
    http.get("/api/subtitles/cleanup", () => HttpResponse.json({ data: [] })),
    http.post("/api/subtitles/cleanup/preview", () =>
      HttpResponse.json(preview, { status: 202 }),
    ),
    http.get("/api/subtitles/cleanup/:id", () => HttpResponse.json(preview)),
  );
};
const previewAndChoose = async () => {
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: "Preview selected media" }),
  );
  await screen.findByText(/Operation movie-operation: ready/);
  await user.click(
    screen.getByRole("radio", { name: /Keep Unknown language.*a.sub/ }),
  );
  return user;
};
const confirm = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(
    screen.getByRole("checkbox", { name: /I reviewed every selected preview/ }),
  );
  await user.click(
    screen.getByRole("button", { name: "Apply confirmed cleanup" }),
  );
};

describe("Subtitle cleanup", () => {
  it("requires explicit unrankable keepers, sends exact choices, refreshes inventory and surfaces restore refusal", async () => {
    register();
    const applyBody = vi.fn();
    const restoreBody = vi.fn();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const complete: CleanupOperation = {
      ...operation(),
      status: "complete",
      backup_path: "/backups/movie-operation",
      result: {
        removed_track_keys: ["hi", "unknown-b"],
        retained_track_keys: ["normal", "forced", "unknown-a"],
        changed_files: ["/media/Film #2 日本語.mkv", "/media/b.sub"],
        restored_files: [],
      },
    };
    server.use(
      http.post("/api/subtitles/cleanup/:id/apply", async ({ request }) => {
        applyBody(await request.json());
        return HttpResponse.json(complete, { status: 202 });
      }),
      http.post("/api/subtitles/cleanup/:id/restore", async ({ request }) => {
        restoreBody(await request.json());
        return HttpResponse.json(
          { error: "Restore refused: video changed after cleanup" },
          { status: 409 },
        );
      }),
    );
    customRender(<SubtitleCleanupView media={[movie]} />);
    const user = userEvent.setup();
    await user.click(
      screen.getByRole("button", { name: "Preview selected media" }),
    );
    await screen.findByText(/Operation movie-operation: ready/);
    expect(
      screen.getByRole("radio", { name: /Keep eng; full dialogue; regular/ }),
    ).toBeChecked();
    expect(
      screen.getByRole("radio", { name: /Keep eng; forced/ }),
    ).toBeChecked();
    expect(
      screen.getByRole("radio", { name: /Keep eng; full dialogue; HI/ }),
    ).toBeDisabled();
    expect(
      screen.getByRole("radio", { name: /Keep Unknown language.*a.sub/ }),
    ).not.toBeChecked();
    expect(
      screen.getByRole("button", { name: "Apply confirmed cleanup" }),
    ).toBeDisabled();
    expect(
      screen.getByText(/Download-match scores are separate/),
    ).toHaveTextContent(/cannot judge wording/);
    expect(screen.getByText(/Required free space/)).toHaveTextContent(
      "2,048 bytes",
    );
    expect(screen.getByText(/Lost caption variants: eng/)).toHaveTextContent(
      "HI / hearing impaired; embedded; pgs",
    );
    expect(screen.getByText(/Rewrite without re-encoding:/)).toHaveTextContent(
      "日本語.mkv",
    );
    await user.click(
      screen.getByRole("radio", { name: /Keep Unknown language.*a.sub/ }),
    );
    expect(screen.getByText(/timing correlation 0.8/)).toHaveTextContent(
      "Download match: 92.0%",
    );
    expect(screen.getByText(/timing correlation 0.4/)).toHaveTextContent(
      "Download match: Not recorded",
    );
    expect(
      screen.getByText("Remove subtitle file: /media/b.idx"),
    ).toBeInTheDocument();
    await confirm(user);
    await screen.findByText("Recorded results");
    expect(applyBody).toHaveBeenCalledWith({
      preview_token: "movie-fingerprint",
      keepers: { eng: "normal", "eng-forced": "forced", unknown: "unknown-a" },
    });
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: [QueryKeys.Movies] }),
    );
    expect(screen.getByText(/Retained tracks:/)).toHaveTextContent(
      "normal, forced, unknown-a",
    );
    expect(
      screen.getByRole("button", { name: `Restore ${movie.title}` }),
    ).toBeDisabled();
    await user.click(
      screen.getByRole("checkbox", { name: /Restore verified backup/ }),
    );
    await user.click(
      screen.getByRole("button", { name: `Restore ${movie.title}` }),
    );
    await screen.findAllByText(/Restore refused: video changed after cleanup/);
    expect(restoreBody).toHaveBeenCalledWith({
      preview_token: "movie-fingerprint",
    });
    invalidate.mockRestore();
  });

  it.each([
    "Preview stale: subtitle inventory changed",
    "Keeper must have the highest timing score",
  ])(
    "blocks rejected previews and requires fresh confirmation: %s",
    async (message) => {
      register();
      const apply = vi.fn();
      server.use(
        http.post("/api/subtitles/cleanup/:id/apply", () => {
          apply();
          return HttpResponse.json({ error: message }, { status: 409 });
        }),
      );
      customRender(<SubtitleCleanupView media={[movie]} />);
      const user = await previewAndChoose();
      await confirm(user);
      await screen.findAllByText(message, { exact: false });
      expect(
        screen.getByRole("button", { name: "Apply confirmed cleanup" }),
      ).toBeDisabled();
      expect(
        screen.getByRole("checkbox", {
          name: /I reviewed every selected preview/,
        }),
      ).not.toBeChecked();
      server.use(
        http.post("/api/subtitles/cleanup/preview", () =>
          HttpResponse.json(
            {
              ...operation(),
              id: "fresh-preview",
              preview_token: "fresh-fingerprint",
            },
            { status: 202 },
          ),
        ),
      );
      await user.click(
        screen.getByRole("button", { name: "Preview selected media" }),
      );
      await screen.findByText(/Operation fresh-preview: ready/);
      expect(
        screen.getByRole("radio", { name: /Keep Unknown language.*a.sub/ }),
      ).not.toBeChecked();
      expect(
        screen.getByRole("button", { name: "Apply confirmed cleanup" }),
      ).toBeDisabled();
      expect(apply).toHaveBeenCalledTimes(1);
    },
  );

  it("sends the chosen absolute audio index after reanalysis and clears confirmation", async () => {
    register();
    const previewBody = vi.fn();
    server.use(
      http.post("/api/subtitles/cleanup/preview", async ({ request }) => {
        previewBody(await request.json());
        return HttpResponse.json(
          previewBody.mock.calls.length > 1
            ? {
                ...operation(),
                id: "new-audio",
                preview_token: "new-audio-token",
                analysis: { ...operation().analysis, audio_stream_index: 4 },
              }
            : operation(),
          { status: 202 },
        );
      }),
    );
    customRender(<SubtitleCleanupView media={[movie]} />);
    const user = await previewAndChoose();
    await user.click(
      screen.getByRole("checkbox", {
        name: /I reviewed every selected preview/,
      }),
    );
    await user.click(
      screen.getByRole("combobox", {
        name: `Audio reference for ${movie.title}`,
      }),
    );
    await user.click(
      await screen.findByRole("option", { hidden: true, name: /Stream 4/ }),
    );
    expect(
      screen.getByRole("button", { name: "Apply confirmed cleanup" }),
    ).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "Preview selected media" }),
    );
    await screen.findByText(/Operation new-audio: ready/);
    expect(previewBody).toHaveBeenNthCalledWith(1, {
      media_type: "movie",
      media_id: 7,
    });
    expect(previewBody).toHaveBeenNthCalledWith(2, {
      media_type: "movie",
      media_id: 7,
      audio_stream_index: 4,
    });
    expect(
      screen.getByRole("checkbox", {
        name: /I reviewed every selected preview/,
      }),
    ).not.toBeChecked();
  });

  it("separates same-number movies and episodes and confirms all bulk previews before writing", async () => {
    register();
    const previewBodies: unknown[] = [];
    const applyBodies: unknown[] = [];
    server.use(
      http.post("/api/subtitles/cleanup/preview", async ({ request }) => {
        const body = (await request.json()) as {
          media_type: "movie" | "series";
          media_id: number;
        };
        previewBodies.push(body);
        const op = operation(body.media_type === "movie" ? movie : episode);
        return HttpResponse.json(
          {
            ...op,
            groups: op.groups.slice(0, 2).map((group) =>
              group.key === "eng"
                ? {
                    ...group,
                    recommended_keeper: null,
                    requires_selection: true,
                  }
                : group,
            ),
            analysis: {
              ...op.analysis,
              tracks: op.analysis?.tracks
                .slice(0, 3)
                .map((track) =>
                  track.key === "hi" ? { ...track, score: 0.8 } : track,
                ),
            },
          },
          { status: 202 },
        );
      }),
      http.post(
        "/api/subtitles/cleanup/:id/apply",
        async ({ request, params }) => {
          applyBodies.push({ id: params.id, body: await request.json() });
          return HttpResponse.json(
            {
              ...operation(params.id === "movie-operation" ? movie : episode),
              status: "applying",
            },
            { status: 202 },
          );
        },
      ),
    );
    customRender(<SubtitleCleanupView media={[movie, episode]} />);
    const user = userEvent.setup();
    await user.click(
      screen.getByRole("button", { name: "Preview selected media" }),
    );
    await screen.findByText(/Operation series-operation: ready/);
    expect(previewBodies).toEqual([
      { media_type: "movie", media_id: 7 },
      { media_type: "series", media_id: 7 },
    ]);
    expect(applyBodies).toEqual([]);
    const movieCard = screen.getByRole("region", {
      name: `${movie.title} cleanup preview`,
    });
    await user.click(
      within(movieCard).getByRole("radio", {
        name: /Keep eng; full dialogue; regular/,
      }),
    );
    const seriesCard = screen.getByRole("region", {
      name: `${episode.title} cleanup preview`,
    });
    await user.click(
      within(seriesCard).getByRole("radio", {
        name: /Keep eng; full dialogue; HI/,
      }),
    );
    await confirm(user);
    await waitFor(() => expect(applyBodies).toHaveLength(2));
    expect(applyBodies).toEqual([
      {
        id: "movie-operation",
        body: {
          preview_token: "movie-fingerprint",
          keepers: { eng: "normal", "eng-forced": "forced" },
        },
      },
      {
        id: "series-operation",
        body: {
          preview_token: "series-fingerprint",
          keepers: { eng: "hi", "eng-forced": "forced" },
        },
      },
    ]);
  });

  it("polls queued analysis and blocks writes on a polling error", async () => {
    register({ ...operation(), status: "queued", analysis: null, groups: [] });
    const gets = vi.fn();
    server.use(
      http.get("/api/subtitles/cleanup/:id", () => {
        gets();
        return HttpResponse.json(
          { error: "Analysis worker unavailable" },
          { status: 503 },
        );
      }),
    );
    customRender(<SubtitleCleanupView media={[movie]} />);
    const user = userEvent.setup();
    await user.click(
      screen.getByRole("button", { name: "Preview selected media" }),
    );
    await screen.findByText(/Operation movie-operation: queued/);
    expect(
      screen.getByRole("button", { name: "Apply confirmed cleanup" }),
    ).toBeDisabled();
    await screen.findByText(
      /Analysis worker unavailable/,
      {},
      { timeout: 5000 },
    );
    expect(gets).toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Apply confirmed cleanup" }),
    ).toBeDisabled();
  });

  it("loads persisted history, excludes other media, and restores through polling", async () => {
    const complete: CleanupOperation = {
      ...operation(),
      status: "complete",
      backup_path: "/backups/op",
      result: {
        removed_track_keys: ["hi"],
        retained_track_keys: ["normal", "forced", "unknown-a"],
        changed_files: ["/media/video.mkv"],
        restored_files: [],
      },
    };
    register(complete);
    const restoring = { current: false };
    server.use(
      http.get("/api/subtitles/cleanup", () =>
        HttpResponse.json({
          data: [complete, { ...complete, media_type: "series", id: "decoy" }],
        }),
      ),
      http.post("/api/subtitles/cleanup/:id/restore", () => {
        restoring.current = true;
        return HttpResponse.json(
          { ...complete, status: "restoring" },
          { status: 202 },
        );
      }),
      http.get("/api/subtitles/cleanup/:id", () =>
        HttpResponse.json(
          restoring.current
            ? {
                ...complete,
                status: "restored",
                result: {
                  ...complete.result,
                  restored_files: ["/media/video.mkv"],
                },
              }
            : complete,
        ),
      ),
    );
    customRender(<SubtitleCleanupView media={[movie]} />);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("combobox", {
        name: `Previous cleanup for ${movie.title}`,
      }),
    );
    expect(
      screen.queryByRole("option", { hidden: true, name: /decoy/ }),
    ).not.toBeInTheDocument();
    await user.click(
      await screen.findByRole("option", {
        hidden: true,
        name: /movie-operation/,
      }),
    );
    await screen.findByText(/Operation movie-operation: complete/);
    await user.click(
      screen.getByRole("checkbox", { name: /Restore verified backup/ }),
    );
    await user.click(
      screen.getByRole("button", { name: `Restore ${movie.title}` }),
    );
    await screen.findByText(
      /Operation movie-operation: restored/,
      {},
      { timeout: 5000 },
    );
    expect(screen.getByText(/Restored files:/)).toHaveTextContent(
      "/media/video.mkv",
    );
  });
  it("requires an explicit tied-score keeper and previews rewrites for multilingual external companions", async () => {
    const tied = operation();
    if (!tied.analysis) throw new Error("Fixture analysis missing");
    tied.analysis.tracks = tied.analysis.tracks.map((track) => ({
      ...track,
      path: "/media/multi.idx",
      companion_paths: ["/media/multi.sub"],
      source: "external",
      score: 0.5,
    }));
    tied.groups[0] = {
      ...tied.groups[0],
      recommended_keeper: null,
      requires_selection: true,
    };
    register(tied);
    customRender(<SubtitleCleanupView media={[movie]} />);
    const user = userEvent.setup();
    await user.click(
      screen.getByRole("button", { name: "Preview selected media" }),
    );
    await screen.findByText(/Operation movie-operation: ready/);
    expect(
      screen.getByRole("radio", { name: /Keep eng; full dialogue; regular/ }),
    ).not.toBeChecked();
    expect(
      screen.getByRole("checkbox", {
        name: /I reviewed every selected preview/,
      }),
    ).toBeDisabled();
    await user.click(
      screen.getByRole("radio", { name: /Keep eng; full dialogue; regular/ }),
    );
    await user.click(
      screen.getByRole("radio", {
        name: /Keep Unknown language; full dialogue; regular/,
      }),
    );
    expect(
      screen.getByText("Rewrite without re-encoding: /media/multi.idx"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Rewrite without re-encoding: /media/multi.sub"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Remove subtitle file:/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", {
        name: /I reviewed every selected preview/,
      }),
    ).toBeEnabled();
  });

  it("retains independent bulk preview failures and cannot confirm an incomplete batch", async () => {
    register();
    server.use(
      http.post("/api/subtitles/cleanup/preview", async ({ request }) => {
        const body = (await request.json()) as { media_type: string };
        return body.media_type === "movie"
          ? HttpResponse.json(
              { error: "Insufficient staging space" },
              { status: 507 },
            )
          : HttpResponse.json(operation(episode), { status: 202 });
      }),
    );
    customRender(<SubtitleCleanupView media={[movie, episode]} />);
    const user = userEvent.setup();
    await user.click(
      screen.getByRole("button", { name: "Preview selected media" }),
    );
    await screen.findByText(/Operation series-operation: ready/);
    expect(
      (await screen.findAllByText(/Insufficient staging space/)).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByRole("button", { name: "Apply confirmed cleanup" }),
    ).toBeDisabled();
  });

  it("shows no tracks for empty inventory and blocks cleanup", async () => {
    const empty = operation();
    if (!empty.analysis) throw new Error("Fixture analysis missing");
    empty.analysis.tracks = [];
    empty.groups = [];
    register(empty);
    customRender(<SubtitleCleanupView media={[movie]} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Preview selected media" }),
    );
    await screen.findByText(
      "No subtitle tracks found. There is nothing to remove.",
    );
    expect(
      screen.getByRole("button", { name: "Apply confirmed cleanup" }),
    ).toBeDisabled();
  });
  it("previews all twenty media independently without skipping empty inventories", async () => {
    register();
    const requests = vi.fn();
    server.use(
      http.post("/api/subtitles/cleanup/preview", async ({ request }) => {
        const body = (await request.json()) as {
          media_type: "movie";
          media_id: number;
        };
        requests(body);
        return HttpResponse.json(
          {
            ...operation({ ...body, title: `Batch ${body.media_id}` }),
            id: `batch-${body.media_id}`,
            groups: [],
            analysis: { ...operation().analysis, tracks: [] },
          },
          { status: 202 },
        );
      }),
    );
    const media: CleanupMedia[] = Array.from({ length: 20 }, (_, index) => ({
      media_type: "movie",
      media_id: index,
      title: `Batch ${index}`,
    }));
    customRender(<SubtitleCleanupView media={media} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Preview selected media" }),
    );
    await screen.findByText("Operation batch-19: ready");
    expect(requests).toHaveBeenCalledTimes(20);
    expect(
      screen.getAllByText(
        "No subtitle tracks found. There is nothing to remove.",
      ),
    ).toHaveLength(20);
    expect(
      screen.getByRole("button", { name: "Apply confirmed cleanup" }),
    ).toBeDisabled();
  });
  it("blocks confirmed ready data when an operation refresh fails", async () => {
    register();
    customRender(<SubtitleCleanupView media={[movie]} />);
    const user = await previewAndChoose();
    await user.click(
      screen.getByRole("checkbox", {
        name: /I reviewed every selected preview/,
      }),
    );
    expect(
      screen.getByRole("button", { name: "Apply confirmed cleanup" }),
    ).toBeEnabled();
    server.use(
      http.get("/api/subtitles/cleanup/:id", () =>
        HttpResponse.json(
          { error: "Preview could not be revalidated" },
          { status: 503 },
        ),
      ),
    );
    await queryClient.invalidateQueries({
      queryKey: [QueryKeys.Subtitles, "cleanup", "movie:7", "movie-operation"],
    });
    await screen.findAllByText(/Preview could not be revalidated/);
    expect(
      screen.getByRole("button", { name: "Apply confirmed cleanup" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("checkbox", {
        name: /I reviewed every selected preview/,
      }),
    ).toBeDisabled();
  });
});
