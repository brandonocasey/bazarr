import { useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Code,
  Divider,
  Group,
  Paper,
  Radio,
  Select,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import {
  useCleanupActions,
  useCleanupHistories,
  useCleanupOperations,
} from "@/apis/hooks/cleanup";
import {
  CleanupMedia,
  cleanupMediaKey,
  CleanupOperation,
  CleanupTrack,
} from "@/apis/raw/cleanup";
import { withModal } from "@/modules/modals";

const previewKey = (operation: CleanupOperation) =>
  `${cleanupMediaKey(operation)}:${operation.id}:${operation.preview_token}`;

const trackDescription = (track: CleanupTrack) =>
  `${track.language ?? "Unknown language"}; ${track.forced ? "forced" : "full dialogue"}; ${track.hearing_impaired ? "HI / hearing impaired" : "regular"}; ${track.source}; ${track.codec ?? "unknown codec"}; stream ${track.stream_index ?? "unknown"}; ${track.path}`;

const bytes = (value: number) => `${value.toLocaleString()} bytes`;

const keeperChoices = (
  operation: CleanupOperation,
  choices: Record<string, string> = {},
) => {
  const retained = new Set(operation.result?.retained_track_keys);
  return Object.fromEntries(
    operation.groups.map((group) => [
      group.key,
      group.track_keys.find((key) => retained.has(key)) ??
        choices[group.key] ??
        (!group.requires_selection
          ? (group.recommended_keeper ??
            (group.track_keys.length === 1 ? group.track_keys[0] : ""))
          : ""),
    ]),
  );
};

const eligibleKeepers = (keys: string[], tracks: Map<string, CleanupTrack>) => {
  const candidates = keys
    .map((key) => tracks.get(key))
    .filter((track): track is CleanupTrack => track !== undefined);
  if (candidates.length !== keys.length) return new Set<string>();
  if (candidates.some((track) => track.score === null)) return new Set(keys);
  const maximum = candidates.reduce(
    (score, track) => Math.max(score, track.score ?? -Infinity),
    -Infinity,
  );
  return new Set(
    candidates
      .filter((track) => track.score === maximum)
      .map((track) => track.key),
  );
};

const OperationPreview = ({
  operation,
  keepers,
  onKeeper,
  disabled,
}: {
  operation: CleanupOperation;
  keepers: Record<string, string>;
  onKeeper: (group: string, track: string) => void;
  disabled: boolean;
}) => {
  const tracks = new Map(
    operation.analysis?.tracks.map((track) => [track.key, track]),
  );
  const retained = new Set(Object.values(keepers));
  const resolved = new Set(
    operation.groups
      .filter((group) => keepers[group.key])
      .flatMap((group) => group.track_keys),
  );
  const units = new Map<string, { paths: string[]; tracks: CleanupTrack[] }>();
  operation.analysis?.tracks.forEach((track) => {
    const paths = [...new Set([track.path, ...track.companion_paths])].sort();
    const key = JSON.stringify([track.source, paths]);
    const unit = units.get(key);
    if (unit) unit.tracks.push(track);
    else units.set(key, { paths, tracks: [track] });
  });
  const changes = [...units.values()].filter((unit) =>
    unit.tracks.some(
      (track) => resolved.has(track.key) && !retained.has(track.key),
    ),
  );

  return (
    <Stack gap="sm">
      {operation.groups.map((group) => {
        const eligible = eligibleKeepers(group.track_keys, tracks);
        return (
          <Paper withBorder p="sm" key={group.key}>
            <Radio.Group
              label={`${group.language ?? "Unknown language"} — ${group.forced ? "forced" : "full dialogue"}`}
              description={
                group.requires_selection
                  ? "Tied or unrankable: explicitly choose one keeper."
                  : "Keep the highest timing score. Lower scores cannot be selected. Normal and HI tracks compete."
              }
              value={keepers[group.key] ?? ""}
              onChange={(track) => onKeeper(group.key, track)}
            >
              <Stack gap="xs" mt="xs">
                {group.track_keys.map((key) => {
                  const track = tracks.get(key);
                  return (
                    <Stack key={key} gap={2}>
                      <Radio
                        disabled={disabled || !track || !eligible.has(key)}
                        value={key}
                        label={`Keep ${track ? trackDescription(track) : `Unavailable track ${key}`}`}
                      />
                      {track && (
                        <Text size="sm" style={{ overflowWrap: "anywhere" }}>
                          {keepers[group.key] === key
                            ? "KEEP"
                            : keepers[group.key]
                              ? "REMOVE"
                              : "Awaiting keeper"}
                          : timing correlation{" "}
                          {track.score === null
                            ? `unrankable (${track.error ?? "no usable signal"})`
                            : String(track.score)}
                          {group.recommended_keeper === key && " · recommended"}
                          {` · Download match: ${track.download_score === null ? "Not recorded" : `${track.download_score.toFixed(1)}%`} (historical metadata only)`}
                        </Text>
                      )}
                    </Stack>
                  );
                })}
              </Stack>
            </Radio.Group>
            {keepers[group.key] && (
              <Text size="sm" mt="xs">
                Lost caption variants:{" "}
                {group.track_keys
                  .filter((key) => key !== keepers[group.key])
                  .map((key) => {
                    const track = tracks.get(key);
                    return track ? trackDescription(track) : key;
                  })
                  .join(" | ") || "none"}
              </Text>
            )}
          </Paper>
        );
      })}
      {!operation.groups.length && (
        <Text>No subtitle tracks found. There is nothing to remove.</Text>
      )}
      <Text fw={600}>Previewed physical changes for these keeper choices</Text>
      {!changes.length && (
        <Text>No files need changing for the resolved groups.</Text>
      )}
      {changes.flatMap((unit) => {
        const action = unit.tracks.some((track) => !resolved.has(track.key))
          ? "Awaiting keeper selection"
          : unit.tracks.some(
                (track) =>
                  track.source === "embedded" || retained.has(track.key),
              )
            ? "Rewrite without re-encoding"
            : "Remove subtitle file";
        return unit.paths.map((path) => (
          <Text key={path} size="sm" style={{ overflowWrap: "anywhere" }}>
            {action}: {path}
          </Text>
        ));
      })}
      <Text>
        Required free space: {bytes(operation.required_space_bytes)}{" "}
        (conservative backup plus replacement estimate).
      </Text>
      <Text>
        Independent verified backups are created before replacement. Backup
        location: {operation.backup_path ?? "assigned when cleanup is applied"}.
      </Text>
      <details>
        <summary>
          Exact preview inputs, fingerprints and scoring settings
        </summary>
        <Stack gap="xs">
          <Text size="sm">Preview token: {operation.preview_token}</Text>
          {operation.files.map((file) => (
            <Text
              key={file.path}
              size="sm"
              style={{ overflowWrap: "anywhere" }}
            >
              {file.path} · {bytes(file.size)} · SHA-256 {file.sha256}
            </Text>
          ))}
          <Code block>
            {JSON.stringify(operation.analysis?.algorithm, null, 2)}
          </Code>
        </Stack>
      </details>
    </Stack>
  );
};

export const SubtitleCleanupView = ({ media }: { media: CleanupMedia[] }) => {
  const [selected, setSelected] = useState(
    () => new Set(media.map(cleanupMediaKey)),
  );
  const [ids, setIds] = useState<Record<string, string>>({});
  const [audio, setAudio] = useState<Record<string, number>>({});
  const [choices, setChoices] = useState<
    Record<string, Record<string, string>>
  >({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [confirmed, setConfirmed] = useState<string | null>(null);
  const [restoreConfirmed, setRestoreConfirmed] = useState<
    Record<string, boolean>
  >({});
  const [running, setRunning] = useState(false);
  const inFlight = useRef(false);
  const histories = useCleanupHistories(media);
  const queries = useCleanupOperations(media, ids);
  const actions = useCleanupActions();
  const selectedMedia = media.filter((item) =>
    selected.has(cleanupMediaKey(item)),
  );
  const busy =
    running ||
    queries.some(
      (query) =>
        !query.isError &&
        query.data &&
        ["queued", "analyzing", "applying", "restoring"].includes(
          query.data.status,
        ),
    );
  const indexes = new Map(
    media.map((item, index) => [cleanupMediaKey(item), index]),
  );
  const previews = queries.filter((_, index) =>
    selected.has(cleanupMediaKey(media[index])),
  );
  const snapshot = JSON.stringify(
    previews.map(
      (query) =>
        query.data && {
          preview: previewKey(query.data),
          keepers: keeperChoices(query.data, choices[previewKey(query.data)]),
        },
    ),
  );
  const canApply =
    selectedMedia.length > 0 &&
    previews.every((query) => {
      const operation = query.data;
      if (
        !operation ||
        query.isError ||
        operation.status !== "ready" ||
        errors[cleanupMediaKey(operation)] ||
        !operation.analysis ||
        !operation.preview_token
      )
        return false;
      if (
        audio[cleanupMediaKey(operation)] !== undefined &&
        audio[cleanupMediaKey(operation)] !==
          operation.analysis.audio_stream_index
      )
        return false;
      const keepers = keeperChoices(operation, choices[previewKey(operation)]);
      const tracks = new Map(
        operation.analysis.tracks.map((track) => [track.key, track]),
      );
      const groupedTracks = new Set(
        operation.groups.flatMap((group) => group.track_keys),
      );
      return (
        operation.groups.length > 0 &&
        operation.groups.every(
          (group) =>
            eligibleKeepers(group.track_keys, tracks).has(keepers[group.key]) &&
            group.track_keys.every((key) => tracks.has(key)),
        ) &&
        operation.analysis.tracks.every((track) => groupedTracks.has(track.key))
      );
    });

  const perform = async (action: "preview" | "apply") => {
    if (
      inFlight.current ||
      (action === "apply" && (!canApply || confirmed !== snapshot))
    )
      return;
    inFlight.current = true;
    setRunning(true);
    setConfirmed(null);
    try {
      for (const item of selectedMedia) {
        const key = cleanupMediaKey(item);
        try {
          const operation = queries[indexes.get(key) ?? -1]?.data;
          const result =
            action === "preview"
              ? await actions.preview.mutateAsync({
                  media: item,
                  audio: audio[key],
                })
              : operation &&
                (await actions.apply.mutateAsync({
                  operation,
                  keepers: keeperChoices(
                    operation,
                    choices[previewKey(operation)],
                  ),
                }));
          if (result) {
            if (cleanupMediaKey(result) !== key)
              throw new Error(
                "Server returned cleanup for different media. Create a new preview.",
              );
            setIds((previous) => ({ ...previous, [key]: result.id }));
            setErrors((previous) => ({ ...previous, [key]: "" }));
          }
        } catch (error) {
          setErrors((previous) => ({
            ...previous,
            [key]: error instanceof Error ? error.message : String(error),
          }));
          if (action === "preview")
            setIds((previous) => {
              const next = { ...previous };
              delete next[key];
              return next;
            });
        }
      }
    } finally {
      inFlight.current = false;
      setRunning(false);
    }
  };

  const restore = async (operation: CleanupOperation) => {
    if (inFlight.current || !restoreConfirmed[previewKey(operation)]) return;
    inFlight.current = true;
    setRunning(true);
    setRestoreConfirmed({});
    const key = cleanupMediaKey(operation);
    try {
      const result = await actions.restore.mutateAsync(operation);
      if (cleanupMediaKey(result) !== key)
        throw new Error("Server returned restore for different media.");
      setIds((previous) => ({ ...previous, [key]: result.id }));
      setErrors((previous) => ({ ...previous, [key]: "" }));
    } catch (error) {
      setErrors((previous) => ({
        ...previous,
        [key]: error instanceof Error ? error.message : String(error),
      }));
    } finally {
      inFlight.current = false;
      setRunning(false);
    }
  };

  return (
    <Stack>
      <Alert title="Review subtitle cleanup" color="yellow">
        All external and embedded tracks, including previously unscored tracks,
        receive a new speech-timing Pearson correlation score from -1 to 1
        against one selected audio reference, using original timing and
        comparable display occupancy. Higher is better; there is no acceptance
        threshold or synchronization. Download-match scores are separate
        metadata and are not used for cleanup ranking. Timing correlation cannot
        judge wording, translation, language correctness, completeness or actual
        content redundancy. Each language keeps one track; forced translations
        stay separate. Normal and hearing-impaired (HI) tracks compete, so HI
        captions can be lost. Unknown and unrankable tracks remain visible and
        require a choice.
      </Alert>
      <Text>
        Previewing only analyzes files. Cleanup removes external files and
        rewrites embedded streams without re-encoding. Review every selected
        media preview before confirming writes. External writers such as
        Sonarr/Radarr require operator coordination.
      </Text>
      {media.length === 0 && <Text>No media selected.</Text>}
      {media.map((item, index) => {
        const key = cleanupMediaKey(item);
        const query = queries[index];
        const operation = query.data;
        const history = histories[index];
        const retainedResults = new Set(operation?.result?.retained_track_keys);
        const removedResults = new Set(operation?.result?.removed_track_keys);
        const audioChanged =
          operation?.analysis &&
          audio[key] !== undefined &&
          audio[key] !== operation.analysis.audio_stream_index;
        return (
          <Paper
            withBorder
            p="md"
            key={key}
            role="region"
            aria-label={`${item.title} cleanup preview`}
          >
            <Stack gap="sm">
              <Checkbox
                label={`${item.title} (${key})`}
                checked={selected.has(key)}
                disabled={busy}
                onChange={(event) => {
                  const checked = event.currentTarget.checked;
                  setSelected((previous) => {
                    const next = new Set(previous);
                    if (checked) next.add(key);
                    else next.delete(key);
                    return next;
                  });
                  setConfirmed(null);
                }}
              />
              {history.isPending && (
                <Text role="status">Loading cleanup history…</Text>
              )}
              {history.isError && (
                <Alert color="red">
                  Could not load cleanup history: {history.error.message}
                  <Button
                    variant="subtle"
                    onClick={() => void history.refetch()}
                  >
                    Retry history
                  </Button>
                </Alert>
              )}
              {history.data && history.data.length > 0 && (
                <Select
                  label={`Previous cleanup for ${item.title}`}
                  placeholder="Select an operation to inspect or restore"
                  disabled={busy}
                  value={ids[key] ?? null}
                  data={history.data
                    .filter((entry) => cleanupMediaKey(entry) === key)
                    .map((entry) => ({
                      value: entry.id,
                      label: `${entry.created_at} · ${entry.status} · ${entry.id}`,
                    }))}
                  onChange={(id) => {
                    if (id) setIds((previous) => ({ ...previous, [key]: id }));
                    setConfirmed(null);
                    setRestoreConfirmed({});
                  }}
                />
              )}
              {errors[key] && (
                <Alert color="red" title="Cleanup request failed">
                  {errors[key]} Create a new preview before applying cleanup
                  again. Restore refuses to overwrite later edits.
                </Alert>
              )}
              {ids[key] && query.isPending && (
                <Text role="status">Loading operation…</Text>
              )}
              {query.isError && (
                <Alert color="red" title="Operation could not be refreshed">
                  {query.error.message}
                  <Button variant="subtle" onClick={() => void query.refetch()}>
                    Retry operation
                  </Button>
                </Alert>
              )}
              {operation && (
                <>
                  <Title order={4}>{operation.title || item.title}</Title>
                  <Text role="status">
                    Operation {operation.id}: {operation.status}
                  </Text>
                  {operation.error && (
                    <Alert color="red">{operation.error}</Alert>
                  )}
                  {operation.analysis && (
                    <>
                      <Select
                        label={`Audio reference for ${item.title}`}
                        description={`Analyzed against stream ${operation.analysis.audio_stream_index}. Changing the reference requires a new preview.`}
                        disabled={busy}
                        value={String(
                          audio[key] ?? operation.analysis.audio_stream_index,
                        )}
                        data={operation.analysis.audio_streams.map(
                          (stream) => ({
                            value: String(stream.index),
                            label: `Stream ${stream.index}: ${stream.language ?? "unknown language"} · ${stream.title ?? "untitled"}${stream.default ? " (default)" : ""}`,
                          }),
                        )}
                        onChange={(value) => {
                          if (value !== null)
                            setAudio((previous) => ({
                              ...previous,
                              [key]: Number(value),
                            }));
                          setConfirmed(null);
                        }}
                      />
                      {audioChanged && (
                        <Alert color="yellow">
                          Audio reference changed. Preview selected media again
                          to recompute scores.
                        </Alert>
                      )}
                      <OperationPreview
                        operation={operation}
                        keepers={keeperChoices(
                          operation,
                          choices[previewKey(operation)],
                        )}
                        disabled={
                          busy || operation.status !== "ready" || query.isError
                        }
                        onKeeper={(group, track) => {
                          setChoices((previous) => ({
                            ...previous,
                            [previewKey(operation)]: {
                              ...previous[previewKey(operation)],
                              [group]: track,
                            },
                          }));
                          setConfirmed(null);
                        }}
                      />
                    </>
                  )}
                  {operation.result && (
                    <Stack gap="xs">
                      <Text fw={600}>Recorded results</Text>
                      <Text>
                        Removed tracks:{" "}
                        {operation.result.removed_track_keys.join(", ") ||
                          "none"}
                      </Text>
                      <Text>
                        Retained tracks:{" "}
                        {operation.result.retained_track_keys.join(", ") ||
                          "none"}
                      </Text>
                      {operation.analysis?.tracks.map((track) => (
                        <Text
                          key={track.key}
                          size="sm"
                          style={{ overflowWrap: "anywhere" }}
                        >
                          {retainedResults.has(track.key)
                            ? "Recorded retained track"
                            : removedResults.has(track.key)
                              ? "Recorded removed track"
                              : "Analyzed track"}
                          : {trackDescription(track)}
                        </Text>
                      ))}
                      <Text>
                        Changed files:{" "}
                        {operation.result.changed_files.join("; ") || "none"}
                      </Text>
                      <Text>
                        Restored files:{" "}
                        {operation.result.restored_files.join("; ") || "none"}
                      </Text>
                      <Text>
                        Subtitle inventory refresh requested from Bazarr.
                      </Text>
                    </Stack>
                  )}
                  {operation.status === "complete" && (
                    <>
                      <Checkbox
                        disabled={busy || query.isError}
                        label={`Restore verified backup for ${item.title}; refuse to overwrite later edits`}
                        checked={
                          restoreConfirmed[previewKey(operation)] ?? false
                        }
                        onChange={(event) => {
                          const checked = event.currentTarget.checked;
                          setRestoreConfirmed((previous) => ({
                            ...previous,
                            [previewKey(operation)]: checked,
                          }));
                        }}
                      />
                      <Button
                        variant="light"
                        disabled={
                          busy ||
                          query.isError ||
                          !restoreConfirmed[previewKey(operation)]
                        }
                        onClick={() => void restore(operation)}
                      >
                        Restore {item.title}
                      </Button>
                    </>
                  )}
                </>
              )}
            </Stack>
          </Paper>
        );
      })}
      <Group>
        <Button
          disabled={busy || selectedMedia.length === 0}
          loading={running}
          onClick={() => void perform("preview")}
        >
          Preview selected media
        </Button>
      </Group>
      <Divider />
      <Checkbox
        label="I reviewed every selected preview, keeper, removal, lost caption variant, rewrite and backup/space cost; apply these exact previews"
        disabled={busy || !canApply}
        checked={confirmed === snapshot}
        onChange={(event) =>
          setConfirmed(event.currentTarget.checked ? snapshot : null)
        }
      />
      <Button
        color="red"
        disabled={busy || !canApply || confirmed !== snapshot}
        onClick={() => void perform("apply")}
      >
        Apply confirmed cleanup
      </Button>
    </Stack>
  );
};

export default withModal(SubtitleCleanupView, "subtitle-cleanup", {
  title: "Subtitle Cleanup",
  size: "xl",
});
