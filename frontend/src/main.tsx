import '@fontsource-variable/inter'
import '@fontsource-variable/inter-tight'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import TokenGate from './components/TokenGate'
import { ToastProvider } from './components/ui'
import './index.css'
import Alerts from './pages/Alerts'
import Blocked from './pages/Blocked'
import DeviceDetail from './pages/DeviceDetail'
import Live from './pages/Live'
import Devices from './pages/Devices'
import Report from './pages/Report'
import Settings from './pages/Settings'
import Setup from './pages/Setup'
import Suggestions from './pages/Suggestions'

const qc = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: true } } })

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <TokenGate>
          <BrowserRouter>
            <Routes>
              <Route element={<Layout />}>
                <Route index element={<Suggestions />} />
                <Route path="live" element={<Live />} />
                <Route path="blocked" element={<Blocked />} />
                <Route path="devices" element={<Devices />} />
                <Route path="devices/:id" element={<DeviceDetail />} />
                <Route path="setup" element={<Setup />} />
                <Route path="alerts" element={<Alerts />} />
                <Route path="report" element={<Report />} />
                <Route path="settings" element={<Settings />} />
              </Route>
            </Routes>
          </BrowserRouter>
        </TokenGate>
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
)
