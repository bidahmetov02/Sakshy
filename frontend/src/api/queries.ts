import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import {
  ApiError,
  api,
  unwrap,
  type Check,
  type CheckIn,
  type CheckPatch,
  type GroupIn,
  type MaintenanceIn,
  type MaintenancePatch,
  type Range,
  type StatusPageIn,
  type StatusPageItemIn,
  type StatusPagePatch,
} from './client'

export const keys = {
  me: ['me'] as const,
  health: ['health'] as const,
  groups: ['groups'] as const,
  checks: (groupId?: number) => ['checks', 'list', groupId ?? 'all'] as const,
  check: (id: number) => ['checks', 'one', id] as const,
  stats: (id: number, range: Range) => ['stats', id, range] as const,
  results: (id: number) => ['results', id] as const,
  incidents: (checkId: number) => ['incidents', checkId] as const,
  maintenance: ['maintenance'] as const,
  statusPages: ['status-pages'] as const,
  statusPage: (id: number) => ['status-pages', id] as const,
  publicPage: (slug: string) => ['public', slug] as const,
  publicStats: (slug: string, item: number) => ['public', slug, 'stats', item] as const,
}

/** The signed-in user, or null when there is no session. */
export function useMe() {
  return useQuery({
    queryKey: keys.me,
    queryFn: async () => {
      try {
        return await unwrap(api.GET('/api/auth/me'))
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null
        throw e
      }
    },
    staleTime: Infinity,
  })
}

export function useHealth() {
  return useQuery({
    queryKey: keys.health,
    queryFn: () => unwrap(api.GET('/api/health')),
    refetchInterval: 30_000,
    retry: false,
  })
}

export function useGroups() {
  return useQuery({ queryKey: keys.groups, queryFn: () => unwrap(api.GET('/api/groups')) })
}

export function useChecks(groupId?: number) {
  return useQuery({
    queryKey: keys.checks(groupId),
    queryFn: () => unwrap(api.GET('/api/checks', { params: { query: { group_id: groupId } } })),
  })
}

export function useCheck(id: number) {
  return useQuery({
    queryKey: keys.check(id),
    queryFn: () => unwrap(api.GET('/api/checks/{check_id}', { params: { path: { check_id: id } } })),
  })
}

export function useStats(id: number, range: Range) {
  return useQuery({
    queryKey: keys.stats(id, range),
    queryFn: () =>
      unwrap(
        api.GET('/api/checks/{check_id}/stats', {
          params: { path: { check_id: id }, query: { range } },
        }),
      ),
    placeholderData: keepPreviousData, // a range switch keeps the old chart until the new one lands
  })
}

const PAGE = 20

export function useResults(id: number) {
  return useInfiniteQuery({
    queryKey: keys.results(id),
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET('/api/checks/{check_id}/results', {
          params: { path: { check_id: id }, query: { limit: PAGE, cursor: pageParam } },
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
  })
}

export function useIncidents(checkId: number) {
  return useInfiniteQuery({
    queryKey: keys.incidents(checkId),
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET('/api/incidents', {
          params: { query: { check_id: checkId, limit: PAGE, cursor: pageParam } },
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
  })
}

/** After any change to a check: its own data and every list that shows it. */
function useRefreshCheck() {
  const qc = useQueryClient()
  return (check?: Check) => {
    if (check) qc.setQueryData(keys.check(check.id), check)
    void qc.invalidateQueries({ queryKey: ['checks'] })
    void qc.invalidateQueries({ queryKey: ['incidents'] })
    void qc.invalidateQueries({ queryKey: keys.groups })
  }
}

export function useCreateCheck() {
  const refresh = useRefreshCheck()
  return useMutation({
    mutationFn: (body: CheckIn) => unwrap(api.POST('/api/checks', { body })),
    onSuccess: refresh,
  })
}

export function useUpdateCheck(id: number) {
  const refresh = useRefreshCheck()
  return useMutation({
    mutationFn: (body: CheckPatch) =>
      unwrap(api.PATCH('/api/checks/{check_id}', { params: { path: { check_id: id } }, body })),
    onSuccess: refresh,
  })
}

export function useDeleteCheck(id: number) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => unwrap(api.DELETE('/api/checks/{check_id}', { params: { path: { check_id: id } } })),
    onSuccess: () => {
      qc.removeQueries({ queryKey: keys.check(id) })
      void qc.invalidateQueries({ queryKey: ['checks'] })
      void qc.invalidateQueries({ queryKey: keys.groups })
    },
  })
}

export type CheckAction = 'pause' | 'resume' | 'run'

export function useCheckAction(id: number) {
  const refresh = useRefreshCheck()
  return useMutation({
    mutationFn: (action: CheckAction) => {
      const params = { params: { path: { check_id: id } } }
      if (action === 'pause') return unwrap(api.POST('/api/checks/{check_id}/pause', params))
      if (action === 'resume') return unwrap(api.POST('/api/checks/{check_id}/resume', params))
      return unwrap(api.POST('/api/checks/{check_id}/run', params))
    },
    onSettled: () => refresh(),
  })
}

// ---------- Groups ----------

/** Group edits change members' effective recipients and maintenance state too. */
function useRefreshGroups() {
  const qc = useQueryClient()
  return () => {
    void qc.invalidateQueries({ queryKey: keys.groups })
    void qc.invalidateQueries({ queryKey: ['checks'] })
    void qc.invalidateQueries({ queryKey: keys.maintenance })
  }
}

export function useCreateGroup() {
  const refresh = useRefreshGroups()
  return useMutation({
    mutationFn: (body: GroupIn) => unwrap(api.POST('/api/groups', { body })),
    onSuccess: refresh,
  })
}

export function useUpdateGroup(id: number) {
  const refresh = useRefreshGroups()
  return useMutation({
    mutationFn: (body: Partial<GroupIn>) =>
      unwrap(api.PATCH('/api/groups/{group_id}', { params: { path: { group_id: id } }, body })),
    onSuccess: refresh,
  })
}

export function useDeleteGroup(id: number) {
  const refresh = useRefreshGroups()
  return useMutation({
    mutationFn: () => unwrap(api.DELETE('/api/groups/{group_id}', { params: { path: { group_id: id } } })),
    onSuccess: refresh,
  })
}

/** Moves checks into a group, or out of any group with null: one PATCH per check. */
export function useSetCheckGroup() {
  const refresh = useRefreshGroups()
  return useMutation({
    mutationFn: ({ checkIds, groupId }: { checkIds: number[]; groupId: number | null }) =>
      Promise.all(
        checkIds.map((id) =>
          unwrap(api.PATCH('/api/checks/{check_id}', { params: { path: { check_id: id } }, body: { group_id: groupId } })),
        ),
      ),
    onSettled: refresh,
  })
}

// ---------- Maintenance ----------

export function useMaintenance() {
  return useQuery({
    queryKey: keys.maintenance,
    queryFn: () => unwrap(api.GET('/api/maintenance')),
    refetchInterval: 60_000, // statuses move with the clock
  })
}

/** A window changes its targets' `in_maintenance`; live updates bring those too. */
function useRefreshMaintenance() {
  const qc = useQueryClient()
  return () => {
    void qc.invalidateQueries({ queryKey: keys.maintenance })
    void qc.invalidateQueries({ queryKey: ['checks'] })
    void qc.invalidateQueries({ queryKey: keys.groups })
  }
}

export function useCreateWindow() {
  const refresh = useRefreshMaintenance()
  return useMutation({
    mutationFn: (body: MaintenanceIn) => unwrap(api.POST('/api/maintenance', { body })),
    onSuccess: refresh,
  })
}

export function useUpdateWindow() {
  const refresh = useRefreshMaintenance()
  return useMutation({
    mutationFn: ({ id, body }: { id: number; body: MaintenancePatch }) =>
      unwrap(api.PATCH('/api/maintenance/{window_id}', { params: { path: { window_id: id } }, body })),
    onSettled: refresh,
  })
}

export function useDeleteWindow() {
  const refresh = useRefreshMaintenance()
  return useMutation({
    mutationFn: (id: number) =>
      unwrap(api.DELETE('/api/maintenance/{window_id}', { params: { path: { window_id: id } } })),
    onSettled: refresh,
  })
}

// ---------- Status pages ----------

export function useStatusPages() {
  return useQuery({ queryKey: keys.statusPages, queryFn: () => unwrap(api.GET('/api/status-pages')) })
}

export function useStatusPage(id: number) {
  return useQuery({
    queryKey: keys.statusPage(id),
    queryFn: () => unwrap(api.GET('/api/status-pages/{page_id}', { params: { path: { page_id: id } } })),
  })
}

export function useCreateStatusPage() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: StatusPageIn) => unwrap(api.POST('/api/status-pages', { body })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: keys.statusPages }),
  })
}

/** Settings and items in one save: the PATCH, then the PUT if the items changed. */
export function useSaveStatusPage(id: number) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ patch, items }: { patch: StatusPagePatch; items?: StatusPageItemIn[] }) => {
      const path = { params: { path: { page_id: id } } }
      await unwrap(api.PATCH('/api/status-pages/{page_id}', { ...path, body: patch }))
      if (items) await unwrap(api.PUT('/api/status-pages/{page_id}/items', { ...path, body: items }))
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: keys.statusPages }),
  })
}

export function useDeleteStatusPage(id: number) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () =>
      unwrap(api.DELETE('/api/status-pages/{page_id}', { params: { path: { page_id: id } } })),
    onSuccess: () => {
      qc.removeQueries({ queryKey: keys.statusPage(id) })
      void qc.invalidateQueries({ queryKey: keys.statusPages })
    },
  })
}

// ---------- Public ----------

export function usePublicPage(slug: string) {
  return useQuery({
    queryKey: keys.publicPage(slug),
    queryFn: () => unwrap(api.GET('/api/public/{slug}', { params: { path: { slug } } })),
  })
}

export function usePublicStats(slug: string, item: number, enabled: boolean) {
  return useQuery({
    queryKey: keys.publicStats(slug, item),
    queryFn: () =>
      unwrap(
        api.GET('/api/public/{slug}/stats', {
          params: { path: { slug }, query: { item, range: 'month' } },
        }),
      ),
    enabled,
    staleTime: 5 * 60_000, // daily bars barely move; the page's live state is pushed separately
  })
}
