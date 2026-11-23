import '@fontsource-variable/inter'
import '@fontsource-variable/inter-tight'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import { DomainViewProvider } from './components/DomainView'
import Layout from './components/Layout'
import { ToastProvider } from './components/ui'
import './index.css'
import Alerts from './pages/Alerts'
import Blocked from './pages/Blocked'
import DeviceDetail from './pages/DeviceDetail'
import Devices from './pages/Devices'
import Landing from './pages/Landing'
import Live from './pages/Live'
import Login from './pages/Login'
import Parental from './pages/Parental'
import Report from './pages/Report'
import Settings from './pages/Settings'
import Setup from './pages/Setup'
import Suggestions from './pages/Suggestions'
import { onAccountChange, useSession } from './session'

const qc = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: true } } })
// Someone else signed in (or out): drop everything cached for the last person before anything renders.
onAccountChange(() => qc.clear())

/** The dashboard needs a sign-in. Signed out, "/" is the homepage and every other page asks to sign in first. */
function RequireSession() {
  const session = useSession()
  const loc = useLocation()
  if (session.status === 'loading') {
    return (
      <div className="grid min-h-dvh place-items-center bg-ink text-muted">
        <Loader2 className="h-5 w-5 animate-spin" aria-label="Loading" />
      </div>
    )
  }
  if (session.status === 'signed-out') {
    if (loc.pathname === '/') return <Landing />
    return <Navigate to={`/login?next=${encodeURIComponent(loc.pathname + loc.search + loc.hash)}`} replace />
  }
  return <Outlet />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <BrowserRouter>
          <Routes>
            <Route path="welcome" element={<Landing />} />
            <Route path="login" element={<Login />} />
            <Route element={<RequireSession />}>
              <Route
                element={
                  <DomainViewProvider>
                    <Layout />
                  </DomainViewProvider>
                }
              >
                <Route index element={<Suggestions />} />
                <Route path="live" element={<Live />} />
                <Route path="blocked" element={<Blocked />} />
                <Route path="devices" element={<Devices />} />
                <Route path="devices/:id" element={<DeviceDetail />} />
                <Route path="setup" element={<Setup />} />
                <Route path="parental" element={<Parental />} />
                <Route path="alerts" element={<Alerts />} />
                <Route path="report" element={<Report />} />
                <Route path="settings" element={<Settings />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Route>
            </Route>
          </Routes>
        </BrowserRouter>
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
)
