import { watch, type Ref } from 'vue'
import type { TabId, ViewMode } from '../types'

const VALID_TABS: TabId[] = ['searches', 'library', 'likes', 'fm']
const VALID_VIEWS: ViewMode[] = ['grid', 'table']

export interface UrlStateRefs {
  activeTab: Ref<TabId>
  viewMode: Ref<ViewMode>
  filterText: Ref<string>
  setTab: (tab: TabId) => void
  setViewMode: (mode: ViewMode) => void
  onFilterInput: (value: string) => void
}

function readParams(): URLSearchParams {
  return new URLSearchParams(window.location.search)
}

function writeParams(tab: TabId, view: ViewMode, q: string) {
  const params = new URLSearchParams()
  if (tab !== 'searches') params.set('tab', tab)
  if (view !== 'grid') params.set('view', view)
  if (q.trim()) params.set('q', q.trim())
  const qs = params.toString()
  const next = qs
    ? `${window.location.pathname}?${qs}`
    : window.location.pathname
  const current = `${window.location.pathname}${window.location.search}`
  if (next !== current) {
    window.history.replaceState(null, '', next)
  }
}

/** Restore tab/view/filter from query string once on boot. */
export function applyUrlStateOnce(refs: UrlStateRefs) {
  const params = readParams()
  const tab = params.get('tab') as TabId | null
  const view = params.get('view') as ViewMode | null
  const q = params.get('q')

  if (tab && VALID_TABS.includes(tab)) {
    refs.setTab(tab)
  }
  if (view && VALID_VIEWS.includes(view)) {
    refs.setViewMode(view)
  }
  if (q != null && q !== '') {
    refs.onFilterInput(q)
  }
}

/** Keep query string in sync with explorer chrome state. */
export function watchUrlState(refs: UrlStateRefs) {
  watch(
    [refs.activeTab, refs.viewMode, refs.filterText],
    ([tab, view, q]) => {
      writeParams(tab, view, q)
    },
  )
}
