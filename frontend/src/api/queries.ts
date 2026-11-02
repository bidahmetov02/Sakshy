import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { ApiError, api, unwrap, type Check, type CheckIn, type CheckPatch, type Range } from './client'

export const keys = {
  me: ['me'] as const,
  health: ['health'] as const,
  groups: ['groups'] as const,
  checks: (groupId?: number) => ['checks', 'list', groupId ?? 'all'] as const,
  check: (id: number) => ['checks', 'one', id] as const,
  stats: (id: number, range: Range) => ['stats', id, range] as const,
  results: (id: number) => ['results', id] as const,
  incidents: (checkId: number) => ['incidents', checkId] as const,
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

export function useCheck(id: number, refetchInterval: number | false = false) {
  return useQuery({
    queryKey: keys.check(id),
    refetchInterval,
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
