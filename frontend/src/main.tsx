import '@fontsource-variable/schibsted-grotesk'
import './styles/tokens.css'
import './styles/app.css'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router'
import { ApiError, setUnauthorizedHandler } from './api/client'
import { keys } from './api/queries'
import { RequireAuth } from './components/RequireAuth'
import { Shell } from './components/Shell'
import { AuthPage } from './pages/AuthPage'
import { CheckDetail } from './pages/CheckDetail'
import { EditCheck, NewCheck } from './pages/CheckFormPage'
import { Dashboard } from './pages/Dashboard'
import { NotFound } from './pages/NotFound'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // A 4xx won't change by asking again; network blips and 5xx get two more tries.
      retry: (failures, error) => !(error instanceof ApiError && error.status < 500) && failures < 2,
      staleTime: 10_000,
    },
  },
})

// Any 401 means the session ended: forget the user, and RequireAuth sends them to sign in.
setUnauthorizedHandler(() => {
  if (queryClient.getQueryData(keys.me)) queryClient.setQueryData(keys.me, null)
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<AuthPage key="login" mode="login" />} />
          <Route path="/signup" element={<AuthPage key="signup" mode="signup" />} />
          <Route
            element={
              <RequireAuth>
                <Shell />
              </RequireAuth>
            }
          >
            <Route index element={<Dashboard />} />
            <Route path="checks/new" element={<NewCheck />} />
            <Route path="checks/:id" element={<CheckDetail />} />
            <Route path="checks/:id/edit" element={<EditCheck />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
