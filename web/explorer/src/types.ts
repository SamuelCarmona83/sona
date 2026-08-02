export type TabId = 'searches' | 'library' | 'likes' | 'fm'
export type ViewMode = 'grid' | 'table'
export type SortKey = 'recent' | 'alpha' | 'duration' | 'plays' | 'size'
export type BannerType = 'info' | 'error' | 'success' | 'warn'
/** Library list grouping in the explorer UI. */
export type LibraryGroupMode = 'flat' | 'video' | 'artist' | 'album'

export interface StatTile {
  value: string
  label: string
  warn?: boolean
}

export interface DiskUsage {
  total_bytes: number
  files: Record<string, number>
  tracks_on_disk: number
  library_path?: string | null
}

export interface DedupePreview {
  duplicate_groups: number
  wasted_bytes: number
  files_to_delete: string[]
  [key: string]: unknown
}

export interface SearchItem {
  query: string
  title: string
  thumbnail?: string
  duration: number
  uploader: string
  webpage_url?: string
  video_id?: string
  cached_at: number
  best_artwork?: string
  cover_url?: string
  /** Times the user requested play (!play / search enqueue). */
  request_count?: number
  trackId?: string
}

export interface LibraryItem {
  trackId: string
  title: string
  artist: string
  thumbnail?: string
  cover_url?: string
  local_cover?: string
  best_artwork?: string | null
  album?: string
  release_date?: string
  genres?: string[]
  enriched_at?: number
  genius_id?: number | null
  genius_url?: string
  lyrics_state?: string
  yt_query?: string
  spotify_id?: string
  video_id?: string
  webpage_url?: string
  duration: number
  play_count: number
  request_count: number
  last_played: number
  last_requested?: number
  cached_at: number
  file_path?: string
  file_size_bytes: number
  on_disk: boolean
  copies?: number
  wasted_bytes?: number
  /**
   * Origin for display:
   * - disk / undefined = local library file or index
   * - fm = shazam capture from !fm / seed radio (may have no file)
   */
  source?: 'disk' | 'fm' | string
  station_name?: string
  /** FM detection count when source=fm */
  detect_count?: number
  match_key?: string
}

/** Hierarchical library sections for artist/album grid views. */
export interface LibraryAlbumGroup {
  key: string
  label: string
  tracks: LibraryItem[]
}

export interface LibraryArtistGroup {
  key: string
  label: string
  albums: LibraryAlbumGroup[]
  trackCount: number
}

export interface LikeItem {
  guildId: string
  userId: string
  track_id?: string
  title: string
  artist: string
  yt_query: string
  spotify_id?: string
  thumbnail?: string
  liked_at: number
  best_artwork?: string
  cover_url?: string
}

export interface FmTrack {
  match_key?: string
  prev_match_key?: string
  artist?: string
  title?: string
  cover_url?: string
  shazam_url?: string
  detected_at?: number
  /** 0-based sequence index from fm_history */
  seq?: number
  [key: string]: unknown
}

export interface FmSession {
  id: string
  guild_id?: string
  stationuuid: string
  station_name: string
  countrycode: string
  tags: string
  stream_url: string
  started_at: number
  ended_at: number | null
  active: boolean
  track_count: number
  duration_sec: number
  tracks: FmTrack[]
}

export interface TransitionEdge {
  from: string
  to: string
  fromLabel: string
  toLabel: string
  count: number
}

export interface TableColumn {
  key: string
  label: string
  wide?: boolean
  narrow?: boolean
  numeric?: boolean
}

export interface TableSort {
  column: string
  dir: 'asc' | 'desc'
}
