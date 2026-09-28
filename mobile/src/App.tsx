import { useEffect, useState } from 'react'
import { BookOpen, Camera, Repeat, UserRound } from 'lucide-react'
import { Toaster } from '@/components/ui/sonner'
import { api, AUTH_EXPIRED_EVENT, getToken, setToken } from '@/lib/api'
import type { User } from '@/types'
import Capture from '@/pages/Capture'
import Login from '@/pages/Login'
import Notebook from '@/pages/Notebook'
import Profile from '@/pages/Profile'
import Review from '@/pages/Review'

type Tab = 'review' | 'capture' | 'notebook' | 'me'

const TABS: { key: Tab; label: string; icon: typeof Repeat }[] = [
  { key: 'review', label: '复习', icon: Repeat },
  { key: 'capture', label: '录题', icon: Camera },
  { key: 'notebook', label: '错题本', icon: BookOpen },
  { key: 'me', label: '我的', icon: UserRound },
]

export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [restoring, setRestoring] = useState(true)
  const [tab, setTab] = useState<Tab>('review')

  // 启动时用本地令牌恢复会话（/auth/me），过期则回落登录页；
  // 本地无令牌时直接进登录页，避免一次必失败的请求
  useEffect(() => {
    if (!getToken()) {
      setRestoring(false)
      return
    }
    api
      .me()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setRestoring(false))
  }, [])

  // 任一请求 401 → 全局登出
  useEffect(() => {
    const onExpired = () => setUser(null)
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired)
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired)
  }, [])

  function logout() {
    setToken(null)
    setUser(null)
  }

  if (restoring) {
    return <div className="flex min-h-dvh items-center justify-center text-sm text-slate-400">加载中…</div>
  }
  if (!user) {
    return (
      <>
        <Login onLogin={setUser} />
        <Toaster position="top-center" richColors />
      </>
    )
  }

  return (
    <div className="mx-auto min-h-dvh max-w-md bg-slate-50">
      {tab === 'review' && <Review />}
      {tab === 'capture' && <Capture onDone={() => setTab('notebook')} />}
      {tab === 'notebook' && <Notebook />}
      {tab === 'me' && <Profile user={user} onLogout={logout} />}

      {/* 底部主导航：四个高频功能，拇指可达 */}
      <nav className="fixed inset-x-0 bottom-0 z-10 mx-auto max-w-md border-t border-slate-200 bg-white pb-[env(safe-area-inset-bottom)]">
        <div className="grid grid-cols-4">
          {TABS.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className={`flex min-h-14 flex-col items-center justify-center gap-0.5 text-xs ${
                tab === key ? 'font-semibold text-blue-600' : 'text-slate-500'
              }`}
            >
              <Icon className="h-5 w-5" />
              {label}
            </button>
          ))}
        </div>
      </nav>
      <Toaster position="top-center" richColors />
    </div>
  )
}
