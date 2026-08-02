/** Shared Tailwind class strings — manpage / terminal explorer chrome. */
export const UI = {
  // Buttons
  btn: 'px-4 py-1 text-sm font-medium text-mute hover:text-ink transition-colors',
  btnActive: 'px-4 py-1 text-sm font-medium bg-ink text-canvas',
  btnPrimary:
    'px-4 py-2 text-sm font-medium text-canvas bg-ink border border-ink rounded hover:opacity-90 disabled:opacity-50',
  btnSecondary:
    'px-4 py-2 text-sm font-medium text-ink border border-black/10 bg-soft rounded hover:bg-canvas disabled:opacity-50',
  btnDanger:
    'px-4 py-2 text-sm font-medium text-danger border border-danger/30 bg-danger/10 rounded hover:bg-danger/15 disabled:opacity-50',
  btnGhost:
    'px-2 py-1 text-sm font-medium text-mute hover:text-ink disabled:opacity-50',

  // Tabs
  tab: 'px-5 py-3 text-sm font-medium text-mute border-b-2 border-transparent hover:text-ink whitespace-nowrap',
  tabActive:
    'px-5 py-3 text-sm font-medium text-ink border-b-2 border-ink whitespace-nowrap',

  // Banners
  banner: 'mb-6 border px-4 py-3 text-sm flex items-start justify-between gap-3',
  bannerInfo: 'border-accent/30 bg-soft text-ink',
  bannerError: 'border-danger/30 bg-soft text-danger',
  bannerSuccess: 'border-ok/30 bg-soft text-ink',
  bannerWarn: 'border-warn/40 bg-soft text-warn',

  // Links & empty
  link: 'text-ink hover:text-accent hover:underline underline-offset-2',
  empty: 'py-12 text-center text-sm text-mute border border-black/10',

  // Cards
  card: 'border border-black/10 bg-canvas overflow-hidden hover:border-mute transition-colors',
  cardThumb:
    'aspect-square bg-soft flex items-center justify-center overflow-hidden',
  cardThumbImg: 'w-full h-full object-cover',
  cardBody: 'p-3',
  cardTitle: 'text-sm font-medium text-ink line-clamp-2 mb-1',
  cardMeta: 'text-[11px] text-mute',
  cardLinks: 'mt-2 text-[11px]',

  // Table
  tableWrap: 'w-full overflow-x-auto border border-black/10',
  th: 'px-2.5 py-2 text-[11px] font-medium text-mute text-left truncate cursor-pointer select-none hover:text-ink',
  thNum:
    'px-2.5 py-2 text-[11px] font-medium text-mute text-right truncate cursor-pointer select-none hover:text-ink',
  td: 'px-2.5 py-2 text-[13px] text-body border-t border-black/10 truncate align-top',
  tdTitle:
    'px-2.5 py-2 text-[13px] text-body border-t border-black/10 whitespace-normal break-words align-top',
  tdNum:
    'px-2.5 py-2 text-[13px] text-body border-t border-black/10 truncate text-right align-top',
  rowHover: 'hover:bg-soft',

  // Shell
  section: 'border border-black/10 p-6',
  statTile: 'border border-black/10 bg-canvas p-4 min-w-0',
  input:
    'h-10 w-full rounded border border-black/10 bg-soft px-3 text-sm text-ink focus:outline-none focus:border-mute',
  select:
    'h-10 rounded border border-black/10 bg-soft px-3 text-sm text-ink focus:outline-none cursor-pointer',
  content: 'max-w-content mx-auto px-5',
} as const

export const TABLE_COLUMNS = {
  searches: [
    { key: 'title', label: 'título', wide: true },
    { key: 'uploader', label: 'artista' },
    { key: 'request_count', label: 'pedidos', numeric: true, narrow: true },
    { key: 'duration', label: 'dur', numeric: true, narrow: true },
    { key: 'cached_at', label: 'fecha', numeric: true },
  ],
  library: [
    { key: 'title', label: 'título', wide: true },
    { key: 'artist', label: 'artista' },
    { key: 'album', label: 'álbum' },
    { key: 'play_count', label: 'plays', numeric: true, narrow: true },
    { key: 'file_size_bytes', label: 'tamaño', numeric: true, narrow: true },
    { key: 'actions', label: '', narrow: true },
  ],
  libraryGrouped: [
    { key: 'title', label: 'título', wide: true },
    { key: 'artist', label: 'artista' },
    { key: 'album', label: 'álbum' },
    { key: 'play_count', label: 'plays', numeric: true, narrow: true },
    { key: 'file_size_bytes', label: 'tamaño', numeric: true, narrow: true },
    { key: 'copies', label: 'dup', numeric: true, narrow: true },
    { key: 'actions', label: '', narrow: true },
  ],
  likes: [
    { key: 'title', label: 'título', wide: true },
    { key: 'artist', label: 'artista' },
    { key: 'liked_at', label: 'fecha', numeric: true, narrow: true },
  ],
} as const

export const COL_WIDTHS: Record<string, string> = {
  wide: '40%',
  artist: '22%',
  default: '14%',
  narrow: '9%',
  actions: '7%',
}

export const GRID_INITIAL_LIMIT = 36

export const DEFAULT_TABLE_SORT: Record<
  string,
  { column: string; dir: 'asc' | 'desc' }
> = {
  searches: { column: 'cached_at', dir: 'desc' },
  library: { column: 'file_size_bytes', dir: 'desc' },
  likes: { column: 'liked_at', dir: 'desc' },
}

export const BANNER_AUTO_DISMISS_MS = 4000
