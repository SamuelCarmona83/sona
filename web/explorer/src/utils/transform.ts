import type {
  DiskUsage,
  FmSession,
  FmTrack,
  LibraryItem,
  LikeItem,
  SearchItem,
  TransitionEdge,
} from '../types'

export function transformSearches(raw: unknown): SearchItem[] {
  if (!raw || typeof raw !== 'object') return []
  return Object.entries(raw as Record<string, Record<string, unknown>>).map(
    ([query, meta]) => ({
      query,
      title: (meta.title as string) || query,
      thumbnail: meta.thumbnail as string | undefined,
      duration: (meta.duration as number) || 0,
      uploader: (meta.uploader as string) || '—',
      webpage_url: meta.webpage_url as string | undefined,
      video_id: meta.video_id as string | undefined,
      cached_at: (meta.cached_at as number) || 0,
    }),
  )
}

export function transformLibrary(
  raw: unknown,
  diskUsage: DiskUsage,
): LibraryItem[] {
  if (!raw || typeof raw !== 'object') return []
  return Object.entries(raw as Record<string, Record<string, unknown>>).map(
    ([trackId, entry]) => {
      const indexedSize = (entry.file_size_bytes as number) || 0
      const fileSize =
        indexedSize > 0 ? indexedSize : diskUsage.files[trackId] || 0
      const onDisk = fileSize > 0 || !!diskUsage.files[trackId]
      const bestArtwork =
        (entry.cover_url as string) || (entry.thumbnail as string) || null
      return {
        trackId,
        title: (entry.title as string) || trackId,
        artist: (entry.artist as string) || '—',
        thumbnail: entry.thumbnail as string | undefined,
        cover_url: (entry.cover_url as string) || '',
        local_cover: (entry.local_cover as string) || '',
        best_artwork: bestArtwork,
        album: (entry.album as string) || '',
        release_date: (entry.release_date as string) || '',
        genres: (entry.genres as string[]) || [],
        enriched_at: (entry.enriched_at as number) || 0,
        genius_id: (entry.genius_id as number | null) ?? null,
        genius_url: (entry.genius_url as string) || '',
        lyrics_state: (entry.lyrics_state as string) || '',
        yt_query: entry.yt_query as string | undefined,
        spotify_id: entry.spotify_id as string | undefined,
        video_id: entry.video_id as string | undefined,
        webpage_url: entry.webpage_url as string | undefined,
        duration: (entry.duration as number) || 0,
        play_count: (entry.play_count as number) || 0,
        request_count: (entry.request_count as number) || 0,
        last_played: (entry.last_played as number) || 0,
        cached_at: (entry.cached_at as number) || 0,
        file_path: entry.file_path as string | undefined,
        file_size_bytes: fileSize,
        on_disk: onDisk,
      }
    },
  )
}

export function transformLikes(raw: unknown): LikeItem[] {
  if (!raw || typeof raw !== 'object') return []
  const flat: LikeItem[] = []
  for (const [guildId, users] of Object.entries(
    raw as Record<string, Record<string, unknown[]>>,
  )) {
    if (!users || typeof users !== 'object') continue
    for (const [userId, tracks] of Object.entries(users)) {
      if (!Array.isArray(tracks)) continue
      for (const t of tracks) {
        const track = t as Record<string, unknown>
        flat.push({
          guildId,
          userId,
          track_id: track.track_id as string | undefined,
          title: (track.title as string) || '—',
          artist: (track.artist as string) || '—',
          yt_query: (track.yt_query as string) || '—',
          spotify_id: track.spotify_id as string | undefined,
          thumbnail: track.thumbnail as string | undefined,
          liked_at: (track.liked_at as number) || 0,
        })
      }
    }
  }
  return flat
}

export function countPlayedIds(raw: unknown): number {
  if (!raw || typeof raw !== 'object') return 0
  let total = 0
  for (const ids of Object.values(raw as Record<string, unknown>)) {
    if (Array.isArray(ids)) total += ids.length
  }
  return total
}

export function transformFmSessions(raw: unknown): FmSession[] {
  const list =
    raw &&
    typeof raw === 'object' &&
    Array.isArray((raw as { sessions?: unknown }).sessions)
      ? ((raw as { sessions: unknown[] }).sessions as Record<string, unknown>[])
      : []
  return list
    .filter((s) => s && typeof s === 'object')
    .map((s) => {
      const tracks = Array.isArray(s.tracks) ? (s.tracks as FmTrack[]) : []
      const started = Number(s.started_at) || 0
      const ended = s.ended_at == null ? null : Number(s.ended_at)
      const durationSec =
        ended != null && started
          ? Math.max(0, ended - started)
          : started
            ? Math.max(0, Date.now() / 1000 - started)
            : 0
      return {
        id: (s.id as string) || '',
        guild_id: s.guild_id as string | undefined,
        stationuuid: (s.stationuuid as string) || '',
        station_name: (s.station_name as string) || 'FM',
        countrycode: (s.countrycode as string) || '',
        tags: (s.tags as string) || '',
        stream_url: (s.stream_url as string) || '',
        started_at: started,
        ended_at: ended,
        active: ended == null,
        track_count:
          s.track_count != null ? Number(s.track_count) : tracks.length,
        duration_sec: durationSec,
        tracks,
      }
    })
    .sort((a, b) => b.started_at - a.started_at)
}

type GroupedLibraryItem = LibraryItem & { all_sizes?: number[] }

export function groupLibraryItems(items: LibraryItem[]): LibraryItem[] {
  const byVideo = new Map<string, GroupedLibraryItem>()
  const noVideo: LibraryItem[] = []

  for (const item of items) {
    if (!item.video_id) {
      noVideo.push({ ...item, copies: 1, wasted_bytes: 0 })
      continue
    }
    const existing = byVideo.get(item.video_id)
    if (!existing) {
      byVideo.set(item.video_id, {
        ...item,
        copies: 1,
        wasted_bytes: 0,
        all_sizes: [item.file_size_bytes || 0],
      })
      continue
    }
    existing.copies = (existing.copies || 1) + 1
    existing.play_count += item.play_count || 0
    existing.request_count += item.request_count || 0
    existing.all_sizes = existing.all_sizes || []
    existing.all_sizes.push(item.file_size_bytes || 0)
    existing.cached_at = Math.max(existing.cached_at || 0, item.cached_at || 0)
    if ((item.file_size_bytes || 0) > (existing.file_size_bytes || 0)) {
      Object.assign(existing, {
        trackId: item.trackId,
        title: item.title,
        thumbnail: item.thumbnail,
        file_size_bytes: item.file_size_bytes,
        on_disk: item.on_disk,
        spotify_id: item.spotify_id || existing.spotify_id,
        best_artwork: item.best_artwork || existing.best_artwork,
        cover_url: item.cover_url || existing.cover_url,
      })
    }
  }

  return [...byVideo.values(), ...noVideo].map((item) => {
    const grouped = item as GroupedLibraryItem
    if (grouped.all_sizes) {
      const sizes = grouped.all_sizes
        .filter((s: number) => s > 0)
        .sort((a: number, b: number) => b - a)
      grouped.file_size_bytes = sizes[0] || 0
      grouped.wasted_bytes =
        sizes.length > 1
          ? sizes.slice(1).reduce((a: number, b: number) => a + b, 0)
          : 0
      delete grouped.all_sizes
    }
    return grouped
  })
}

export function buildTransitions(tracks: FmTrack[]): TransitionEdge[] {
  const counts = new Map<string, number>()
  const labels = new Map<string, string>()
  for (const t of tracks) {
    if (!t || !t.prev_match_key || !t.match_key) continue
    const edgeKey = `${t.prev_match_key}→${t.match_key}`
    counts.set(edgeKey, (counts.get(edgeKey) || 0) + 1)
  }
  for (const t of tracks) {
    if (t && t.match_key) {
      labels.set(t.match_key, `${t.artist || '?'} — ${t.title || '?'}`)
    }
  }
  return [...counts.entries()]
    .map(([edge, count]) => {
      const [from, to] = edge.split('→')
      return {
        from,
        to,
        fromLabel: labels.get(from) || from,
        toLabel: labels.get(to) || to,
        count,
      }
    })
    .sort(
      (a, b) =>
        b.count - a.count || a.fromLabel.localeCompare(b.fromLabel),
    )
}
