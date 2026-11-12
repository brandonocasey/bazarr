import { UseMutationResult } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw/http";
import { beforeEach, describe, expect, it, vi, vitest } from "vitest";
import { AppColumnDef as ColumnDef } from "@/components/tables/features";
import { customRender, screen, within } from "@/tests";
import server from "@/tests/mocks/node";
import ItemView from "./ItemView";

vi.mock("@/apis/hooks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/apis/hooks")>()),
  useLanguageProfiles: () => ({ data: [] }),
  useLanguages: () => ({ data: [] }),
}));

const item = {
  title: "My Show",
  sonarrSeriesId: 1,
  tags: [],
} as unknown as Item.Series;

const columns: ColumnDef<Item.Series>[] = [
  { header: "Name", accessorKey: "title" },
];

const filterConfig = {
  sortFields: [{ value: "title", label: "Name" }],
  filters: {
    monitored: true,
    missing: true,
    profile: true,
    tags: true,
    ended: true,
  },
};

const queryKey = ["test-item-view"];

const mockUseAllItems = () => ({
  data: undefined,
  isSuccess: false,
  isFetching: false,
  isError: false,
});

const mockMutation = {
  mutateAsync: vitest.fn(),
} as unknown as UseMutationResult<void, unknown, FormType.ModifyItem>;

const buildQueryFn = (total = 1): RangeQuery<Item.Series> => {
  return vitest.fn(() => Promise.resolve({ data: [item], total }));
};

const renderPoster = (item: Item.Series) => {
  return (
    <div key={item.sonarrSeriesId} data-testid="poster-card">
      {item.title}
    </div>
  );
};

describe("ItemView", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.history.pushState({}, "", "/");
  });

  it("renders the table view by default", async () => {
    customRender(
      <ItemView
        queryKey={queryKey}
        queryFn={buildQueryFn()}
        columns={columns}
        useAllItems={mockUseAllItems}
        modifyMutation={mockMutation}
        viewModeKey="test-view-mode"
        renderPoster={renderPoster}
        filterConfig={filterConfig}
      ></ItemView>,
    );

    expect(
      await screen.findByRole("columnheader", { name: "Name" }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("poster-card")).not.toBeInTheDocument();
  });

  it("switches to the poster view and persists the selection", async () => {
    customRender(
      <ItemView
        queryKey={queryKey}
        queryFn={buildQueryFn()}
        columns={columns}
        useAllItems={mockUseAllItems}
        modifyMutation={mockMutation}
        viewModeKey="test-view-mode"
        renderPoster={renderPoster}
        filterConfig={filterConfig}
      ></ItemView>,
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "View: table" }),
    );
    await userEvent.click(await screen.findByText("Poster view"));

    expect(await screen.findByTestId("poster-card")).toBeInTheDocument();
    expect(screen.queryByRole("columnheader")).not.toBeInTheDocument();
    expect(window.localStorage.getItem("test-view-mode")).toBe(
      JSON.stringify("poster"),
    );
  });

  it("restores the persisted poster view on mount", async () => {
    window.localStorage.setItem("test-view-mode", JSON.stringify("poster"));

    customRender(
      <ItemView
        queryKey={queryKey}
        queryFn={buildQueryFn()}
        columns={columns}
        useAllItems={mockUseAllItems}
        modifyMutation={mockMutation}
        viewModeKey="test-view-mode"
        renderPoster={renderPoster}
        filterConfig={filterConfig}
      ></ItemView>,
    );

    expect(await screen.findByTestId("poster-card")).toBeInTheDocument();
  });

  it("switches back to the table view", async () => {
    window.localStorage.setItem("test-view-mode", JSON.stringify("poster"));

    customRender(
      <ItemView
        queryKey={queryKey}
        queryFn={buildQueryFn()}
        columns={columns}
        useAllItems={mockUseAllItems}
        modifyMutation={mockMutation}
        viewModeKey="test-view-mode"
        renderPoster={renderPoster}
        filterConfig={filterConfig}
      ></ItemView>,
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "View: poster" }),
    );
    await userEvent.click(await screen.findByText("Table view"));

    expect(
      await screen.findByRole("columnheader", { name: "Name" }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("poster-card")).not.toBeInTheDocument();
  });

  it("sorts by clicking the column header", async () => {
    customRender(
      <ItemView
        queryKey={queryKey}
        queryFn={buildQueryFn()}
        columns={columns}
        useAllItems={mockUseAllItems}
        modifyMutation={mockMutation}
        viewModeKey="test-view-mode"
        renderPoster={renderPoster}
        filterConfig={filterConfig}
      ></ItemView>,
    );

    const header = await screen.findByRole("columnheader", { name: "Name" });

    await userEvent.click(header);
    expect(window.location.search).toContain("sort_by=title");
    expect(window.location.search).toContain("sort_order=asc");

    await userEvent.click(header);
    expect(window.location.search).toContain("sort_order=desc");
  });

  it("sorts from the poster view dropdown, flipping the default field", async () => {
    window.localStorage.setItem("test-view-mode", JSON.stringify("poster"));

    customRender(
      <ItemView
        queryKey={queryKey}
        queryFn={buildQueryFn()}
        columns={columns}
        useAllItems={mockUseAllItems}
        modifyMutation={mockMutation}
        viewModeKey="test-view-mode"
        renderPoster={renderPoster}
        filterConfig={filterConfig}
      ></ItemView>,
    );

    await screen.findByTestId("poster-card");

    // "Name" is the default sort field, so the first click flips its direction
    await userEvent.click(screen.getByRole("button", { name: /Sort/ }));
    await screen.findByText("Name");
    await userEvent.click(screen.getByText("Name"));
    expect(window.location.search).toContain("sort_by=title");
    expect(window.location.search).toContain("sort_order=desc");
  });

  it("flips the sort direction of the active field", async () => {
    window.localStorage.setItem("test-view-mode", JSON.stringify("poster"));
    window.history.pushState({}, "", "/?sort_by=title&sort_order=desc");

    customRender(
      <ItemView
        queryKey={queryKey}
        queryFn={buildQueryFn()}
        columns={columns}
        useAllItems={mockUseAllItems}
        modifyMutation={mockMutation}
        viewModeKey="test-view-mode"
        renderPoster={renderPoster}
        filterConfig={filterConfig}
      ></ItemView>,
    );

    await screen.findByTestId("poster-card");

    await userEvent.click(screen.getByRole("button", { name: /Sort/ }));
    await screen.findByText("Name");
    await userEvent.click(screen.getByText("Name"));
    expect(window.location.search).toContain("sort_order=asc");
  });

  it("resets the page param when a filter changes", async () => {
    window.history.pushState({}, "", "/?page=2");

    customRender(
      <ItemView
        queryKey={queryKey}
        queryFn={buildQueryFn()}
        columns={columns}
        useAllItems={mockUseAllItems}
        modifyMutation={mockMutation}
        viewModeKey="test-view-mode"
        renderPoster={renderPoster}
        filterConfig={filterConfig}
      ></ItemView>,
    );

    await screen.findByRole("columnheader", { name: "Name" });
    await userEvent.click(screen.getByText("Monitored"));

    expect(window.location.search).toContain("monitored=true");
    expect(window.location.search).not.toContain("page=");
  });

  it("clears a filter when All is selected", async () => {
    window.history.pushState({}, "", "/?monitored=true");

    customRender(
      <ItemView
        queryKey={queryKey}
        queryFn={buildQueryFn()}
        columns={columns}
        useAllItems={mockUseAllItems}
        modifyMutation={mockMutation}
        filterConfig={filterConfig}
      ></ItemView>,
    );

    await screen.findByRole("columnheader", { name: "Name" });

    const monitored = screen.getByRole("radiogroup", { name: "Monitored" });
    await userEvent.click(within(monitored).getByText("All"));

    expect(window.location.search).not.toContain("monitored");
  });

  it("commits the tags filter only when the dropdown closes", async () => {
    customRender(
      <ItemView
        queryKey={["test-item-view-tags"]}
        queryFn={buildQueryFn()}
        columns={columns}
        useAllItems={mockUseAllItems}
        modifyMutation={mockMutation}
        filterConfig={filterConfig}
        useTags={() => ({ data: ["anime"] })}
      ></ItemView>,
    );

    await screen.findByRole("columnheader", { name: "Name" });

    const tagsInput = screen.getByPlaceholderText("Tags");
    await userEvent.click(tagsInput);
    await userEvent.click(
      await screen.findByRole("option", { hidden: true, name: "anime" }),
    );

    // picking an option must not push a URL change (it closes the dropdown)
    expect(window.location.search).not.toContain("tags");

    await userEvent.keyboard("{Escape}");
    expect(window.location.search).toContain("tags=anime");
  });

  it("does not show the toggle without poster support", async () => {
    customRender(
      <ItemView
        queryKey={queryKey}
        queryFn={buildQueryFn()}
        columns={columns}
        useAllItems={mockUseAllItems}
        modifyMutation={mockMutation}
        filterConfig={filterConfig}
      ></ItemView>,
    );

    expect(screen.queryByText("Poster view")).not.toBeInTheDocument();
    expect(
      await screen.findByRole("columnheader", { name: "Name" }),
    ).toBeInTheDocument();
  });
  it("opens bulk cleanup only for selected movies with the correct media identity", async () => {
    const body = vi.fn();
    server.use(
      http.get("/api/subtitles/cleanup", () => HttpResponse.json({ data: [] })),
      http.post("/api/subtitles/cleanup/preview", async ({ request }) => {
        body(await request.json());
        return HttpResponse.json(
          {
            id: "bulk-movie7",
            media_type: "movie",
            media_id: 7,
            title: "Selected Movie",
            status: "queued",
            analysis: null,
            groups: [],
            files: [],
            preview_token: "bulk-token",
            required_space_bytes: 0,
            backup_path: null,
            result: null,
            error: null,
            created_at: "2026-11-12",
          },
          { status: 202 },
        );
      }),
    );
    const selectedMovie = {
      tags: [],
      radarrId: 7,
      title: "Selected Movie",
    } as unknown as Item.Movie;
    const decoyMovie = { ...selectedMovie, radarrId: 8, title: "Other Movie" };
    const movieColumns: ColumnDef<Item.Movie>[] = [
      { header: "Name", accessorKey: "title" },
    ];
    const movieQuery: RangeQuery<Item.Movie> = () =>
      Promise.resolve({ data: [selectedMovie, decoyMovie], total: 2 });
    customRender(
      <ItemView
        queryKey={["cleanup-movies"]}
        queryFn={movieQuery}
        columns={movieColumns}
        useAllItems={mockUseAllItems}
        modifyMutation={mockMutation}
        filterConfig={filterConfig}
        cleanupMediaType="movie"
      />,
    );
    const user = userEvent.setup();
    await screen.findByRole("row", { name: "Selected Movie" });
    await user.click(screen.getByRole("button", { name: "Select" }));
    expect(
      screen.getByRole("button", { name: "Cleanup subtitles" }),
    ).toBeDisabled();
    await user.click(
      within(screen.getByRole("row", { name: "Selected Movie" })).getByRole(
        "checkbox",
      ),
    );
    await user.click(screen.getByRole("button", { name: "Cleanup subtitles" }));
    await user.click(
      await screen.findByRole("button", { name: "Preview selected media" }),
    );
    await screen.findByText(/Operation bulk-movie7: queued/);
    expect(body).toHaveBeenCalledExactlyOnceWith({
      media_type: "movie",
      media_id: 7,
    });
  });
});
