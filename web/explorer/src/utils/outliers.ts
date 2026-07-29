import type { LibraryItem } from '../types'

const OUTLIER_MAX_BYTES = 25 * 1024 * 1024
const OUTLIER_MAX_DURATION = 900
const LIVE_RADIO_TITLE_RE =
  /24\s*\/\s*7|non[\s-]?stop|nonstop|live\s*stream|livestream|listening\s+party|radio\s+(?:hits|mix|station|stream|live|24)|(?:classic\s+)?(?:rock|jazz|pop|hits)\s+radio/i

export function isLibraryOutlier(item: LibraryItem | null | undefined): boolean {
  if (!item) return false
  if ((item.file_size_bytes || 0) > OUTLIER_MAX_BYTES) return true
  const dur = item.duration || 0
  if (dur === 0 && item.on_disk) return true
  if (dur > OUTLIER_MAX_DURATION) return true
  const blob = `${item.title || ''} ${item.yt_query || ''}`
  if (LIVE_RADIO_TITLE_RE.test(blob)) return true
  return false
}

export { OUTLIER_MAX_BYTES }
