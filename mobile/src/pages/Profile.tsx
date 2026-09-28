import { useEffect, useState } from 'react'
import { Flame, LogOut, Trophy } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { api, ApiError } from '@/lib/api'
import type { DashboardStats, User } from '@/types'

export default function Profile({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [stats, setStats] = useState<DashboardStats | null>(null)

  useEffect(() => {
    api
      .dashboard()
      .then(setStats)
      .catch((err) => toast.error(err instanceof ApiError ? err.message : '加载失败'))
  }, [])

  const cards = stats
    ? [
        { label: '累计错题', value: stats.total, color: 'border-t-blue-600' },
        { label: '待复习', value: stats.due, color: 'border-t-amber-500' },
        { label: '已复习', value: stats.reviewed, color: 'border-t-green-600' },
        { label: '已掌握', value: stats.mastered, color: 'border-t-teal-600' },
      ]
    : []

  return (
    <div className="px-4 pb-24 pt-6">
      <header className="mb-5 flex items-center gap-3">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-gradient-to-br from-blue-600 to-teal-600 text-lg font-bold text-white">
          {user.username.slice(0, 2).toUpperCase()}
        </div>
        <div>
          <h1 className="text-lg font-bold text-slate-900">{user.username}</h1>
          <p className="text-xs text-slate-500">{user.role === 'teacher' ? '教师' : '学生'}</p>
        </div>
      </header>

      {stats ? (
        <>
          <div className="mb-3 grid grid-cols-2 gap-2.5">
            {cards.map((c) => (
              <Card key={c.label} className={`border-t-[3px] ${c.color}`}>
                <CardContent className="p-3.5">
                  <p className="text-2xl font-bold text-slate-900">{c.value}</p>
                  <p className="text-xs text-slate-500">{c.label}</p>
                </CardContent>
              </Card>
            ))}
          </div>
          <Card>
            <CardContent className="flex items-center justify-around p-4 text-sm text-slate-600">
              <span className="flex items-center gap-1.5">
                <Flame className="h-4 w-4 text-orange-500" /> 连续学习 {stats.streak} 天
              </span>
              <span className="flex items-center gap-1.5">
                <Trophy className="h-4 w-4 text-amber-500" /> 已掌握 {stats.mastered} 题
              </span>
            </CardContent>
          </Card>
        </>
      ) : (
        <div className="grid grid-cols-2 gap-2.5">
          <Skeleton className="h-20" />
          <Skeleton className="h-20" />
          <Skeleton className="h-20" />
          <Skeleton className="h-20" />
        </div>
      )}

      <Button
        variant="outline"
        className="mt-8 w-full text-red-600"
        onClick={() => {
          api && onLogout()
        }}
      >
        <LogOut className="mr-2 h-4 w-4" /> 退出登录
      </Button>
    </div>
  )
}
