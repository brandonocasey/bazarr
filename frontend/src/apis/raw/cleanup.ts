import client from "./client";

export interface CleanupMedia {
  media_type: "movie" | "series";
  media_id: number;
  title: string;
}

export interface CleanupTrack {
  key: string;
  source: "embedded" | "external";
  path: string;
  companion_paths: string[];
  stream_index: number | null;
  language: string | null;
  forced: boolean;
  hearing_impaired: boolean;
  codec: string | null;
  score: number | null;
  download_score: number | null;
  error: string | null;
}

export interface CleanupGroup {
  key: string;
  language: string | null;
  forced: boolean;
  track_keys: string[];
  recommended_keeper: string | null;
  requires_selection: boolean;
}

export interface CleanupOperation extends CleanupMedia {
  id: string;
  status:
    | "queued"
    | "analyzing"
    | "ready"
    | "applying"
    | "complete"
    | "restoring"
    | "restored"
    | "failed";
  preview_token: string;
  analysis: {
    video_path: string;
    duration: number;
    audio_stream_index: number | null;
    audio_streams: {
      index: number;
      language: string | null;
      title: string | null;
      default: boolean;
    }[];
    tracks: CleanupTrack[];
    algorithm: Record<string, unknown>;
  } | null;
  groups: CleanupGroup[];
  files: { path: string; size: number; sha256: string }[];
  required_space_bytes: number;
  backup_path: string | null;
  result: {
    removed_track_keys: string[];
    retained_track_keys: string[];
    changed_files: string[];
    restored_files: string[];
  } | null;
  error: string | null;
  created_at: string;
}

export interface CleanupOperationStatus {
  id: string;
  operation: CleanupOperation | null;
  error: string | null;
}

export const cleanupMediaKey = (media: CleanupMedia) =>
  `${media.media_type}:${media.media_id}`;

const prefix = "subtitles/cleanup";
const cleanup = {
  async preview(media: CleanupMedia, audioStreamIndex?: number) {
    const { media_type, media_id } = media;
    const response = await client.axios.post<CleanupOperation>(
      `${prefix}/preview`,
      { media_type, media_id, audio_stream_index: audioStreamIndex },
    );
    return response.data;
  },
  async list(media: CleanupMedia) {
    const { media_type, media_id } = media;
    const response = await client.axios.get<{ data: CleanupOperation[] }>(
      prefix,
      { params: { media_type, media_id } },
    );
    return response.data.data;
  },
  async get(id: string) {
    const response = await client.axios.get<CleanupOperation>(
      `${prefix}/${encodeURIComponent(id)}`,
    );
    return response.data;
  },
  async status(ids: string[], signal: AbortSignal) {
    const response = await client.axios.post<{
      data: CleanupOperationStatus[];
    }>(`${prefix}/status`, { operation_ids: ids }, { signal });
    const statuses = response.data?.data;
    if (
      !Array.isArray(statuses) ||
      statuses.some(
        (entry) =>
          !entry ||
          typeof entry.id !== "string" ||
          (entry.error !== null && typeof entry.error !== "string") ||
          (entry.operation !== null &&
            (typeof entry.operation !== "object" ||
              typeof entry.operation.id !== "string")),
      )
    )
      throw new Error("Invalid cleanup status response. Retry the operation.");
    return statuses;
  },
  async apply(
    id: string,
    previewToken: string,
    keepers: Record<string, string>,
  ) {
    const response = await client.axios.post<CleanupOperation>(
      `${prefix}/${encodeURIComponent(id)}/apply`,
      { preview_token: previewToken, keepers },
    );
    return response.data;
  },
  async restore(id: string, previewToken: string) {
    const response = await client.axios.post<CleanupOperation>(
      `${prefix}/${encodeURIComponent(id)}/restore`,
      { preview_token: previewToken },
    );
    return response.data;
  },
};

export default cleanup;
