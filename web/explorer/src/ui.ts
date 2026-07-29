/** Shared Tailwind class strings — mirrors the vanilla explorer UI object. */
export const UI = {
  btn: 'px-4 py-1 text-sm font-medium text-mute hover:text-ink',
  btnActive: 'px-4 py-1 text-sm font-medium bg-ink text-canvas',
  btnDanger:
    'px-4 py-2 text-sm font-medium text-danger border border-danger/30 bg-danger/10 rounded hover:bg-danger/15 disabled:opacity-50',
  tab: 'px-5 py-3 text-sm font-medium text-mute border-b-2 border-transparent hover:text-ink',
  tabActive:
    'px-5 py-3 text-sm font-medium text-ink border-b-2 border-ink',
  banner: 'mb-6 border px-4 py-3 text-sm',
  bannerInfo: 'border-accent/30 bg-soft text-ink',
  bannerError: 'border-danger/30 bg-soft text-danger',
  link: 'text-ink hover:text-accent hover:underline underline-offset-2',
  empty: 'py-12 text-center text-sm text-mute border border-black/10',
  card: 'border border-black/10 bg-canvas overflow-hidden hover:border-mute',
  cardThumb:
    'aspect-square bg-soft flex items-center justify-center overflow-hidden',
  cardThumbImg: 'w-full h-full object-cover',
  cardBody: 'p-3',
  cardTitle: 'text-sm font-medium text-ink line-clamp-2 mb-1',
  cardMeta: 'text-[11px] text-mute',
  cardLinks: 'mt-2 text-[11px]',
  tableWrap: 'w-full overflow-hidden border border-black/10',
  th: 'px-2.5 py-2 text-[11px] font-medium text-mute text-left truncate cursor-pointer select-none hover:text-ink',
  thNum:
    'px-2.5 py-2 text-[11px] font-medium text-mute text-right truncate cursor-pointer select-none hover:text-ink',
  td: 'px-2.5 py-2 text-[13px] text-body border-t border-black/10 truncate align-top',
  tdTitle:
    'px-2.5 py-2 text-[13px] text-body border-t border-black/10 whitespace-normal break-words align-top',
  tdNum:
    'px-2.5 py-2 text-[13px] text-body border-t border-black/10 truncate text-right align-top',
  rowHover: 'hover:bg-soft',
}

export const TABLE_COLUMNS = {
  searches: [
    { key: 'title', label: 'título', wide: true },
    { key: 'duration', label: 'dur', numeric: true, narrow: true },
    { key: 'cached_at', label: 'fecha', numeric: true },
  ],
  library: [
    { key: 'title', label: 'título', wide: true },
    { key: 'artist', label: 'artista' },
    { key: 'play_count', label: 'plays', numeric: true, narrow: true },
    { key: 'file_size_bytes', label: 'tamaño', numeric: true, narrow: true },
    { key: 'actions', label: '', narrow: true },
  ],
  libraryGrouped: [
    { key: 'title', label: 'título', wide: true },
    { key: 'artist', label: 'artista' },
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
