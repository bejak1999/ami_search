import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from '@/components/AppShell'
import { Spinner } from '@/components/ui'
import { useAuth } from '@/lib/auth'
import { AdminPage } from '@/pages/Admin'
import { AlertsPage } from '@/pages/Alerts'
import { CollectionPage } from '@/pages/Collection'
import { DashboardPage } from '@/pages/Dashboard'
import { DiscoverPage } from '@/pages/Discover'
import { ItemDetailPage } from '@/pages/ItemDetail'
import { LoginPage } from '@/pages/Login'
import { SearchPage } from '@/pages/Search'
import { SettingsPage } from '@/pages/Settings'
import { WatchesPage } from '@/pages/Watches'

export default function App() {
  const { user, loading } = useAuth()

  if (loading) {
    return (
      <div className="grid min-h-dvh place-items-center bg-canvas">
        <Spinner className="h-6 w-6 text-accent" />
      </div>
    )
  }

  if (!user) return <LoginPage />

  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<DashboardPage />} />
        <Route path="search" element={<SearchPage />} />
        <Route path="discover" element={<DiscoverPage />} />
        <Route path="watches" element={<WatchesPage />} />
        <Route path="alerts" element={<AlertsPage />} />
        <Route path="collection" element={<CollectionPage />} />
        <Route path="item/:itemId" element={<ItemDetailPage />} />
        <Route path="settings" element={<SettingsPage />} />
        {user.role === 'admin' && <Route path="admin" element={<AdminPage />} />}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
