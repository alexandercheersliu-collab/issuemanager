import { useCallback, useEffect, useRef, useState } from 'react'
import { Search } from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { api, ApiError } from '@/lib/api'
import { splitStem, type Question } from '@/types'
import DetectionPanel from '@/components/DetectionPanel'
import MathText from '@/components/MathText'

const PAGE_SIZE = 15

function snippet(q: Question): string {
  const text = splitStem(q.content_markdown).stem
  return text.replace(/\s+/g, ' ').slice(0, 60)
}

export default function Notebook() {
  const [keyword, setKeyword] = useState('')
  const [items, setItems] = useState<Question[]>([])
  const [loading, setLoading] = useState(true)
  const [hasMore, setHasMore] = useState(false)
  const [detail, setDetail] = useState<Question | null>(null)
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(null)

  const load = useCallback(async (kw: string, offset: number, append: boolean) => {
    setLoading(true)
    try {
      const page = await api.listQuestions({ keyword: kw || undefined, offset, limit: PAGE_SIZE })
      setItems((prev) => (append ? [...prev, ...page] : page))
      // 语义检索无精确总数（服务端此时不返回 X-Total-Count），按「不足一页即到底」判断
      setHasMore(page.length === PAGE_SIZE)
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load('', 0, false)
  }, [load])

  function onSearch(value: string) {
    setKeyword(value)
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => load(value.trim(), 0, false), 400)
  }

  return (
    <div className="px-4 pb-24 pt-6">
      <header className="mb-4">
        <h1 className="text-xl font-bold text-slate-900">错题本</h1>
        <p className="text-sm text-slate-500">自然语言搜索，例如「判别式没掌握的题」</p>
      </header>

      <div className="relative mb-4">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
        <Input
          className="pl-9"
          placeholder="搜索错题…"
          value={keyword}
          onChange={(e) => onSearch(e.target.value)}
        />
      </div>

      <div className="space-y-2.5">
        {items.map((q) => (
          <Card key={q.id} className="active:bg-slate-50" onClick={() => setDetail(q)}>
            <CardContent className="p-3.5">
              <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
                <Badge variant="secondary" className="bg-blue-50 text-blue-700">
                  {q.difficulty}
                </Badge>
                {q.tags.slice(0, 3).map((t) => (
                  <Badge key={t} variant="outline" className="text-slate-600">
                    {t}
                  </Badge>
                ))}
                {q.demotion_state === 'demoted' && (
                  <Badge variant="secondary" className="bg-amber-50 text-amber-700">
                    🏅 已降级
                  </Badge>
                )}
              </div>
              <p className="line-clamp-2 text-sm text-slate-700">{snippet(q)}…</p>
            </CardContent>
          </Card>
        ))}
      </div>

      {loading && (
        <div className="mt-3 space-y-2.5">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
        </div>
      )}
      {!loading && items.length === 0 && (
        <p className="py-16 text-center text-sm text-slate-400">没有匹配的错题</p>
      )}
      {hasMore && !loading && (
        <Button
          variant="outline"
          className="mt-4 w-full"
          onClick={() => load(keyword.trim(), items.length, true)}
        >
          加载更多
        </Button>
      )}

      <Dialog open={detail !== null} onOpenChange={(open) => !open && setDetail(null)}>
        <DialogContent className="max-h-[80dvh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="text-base">错题详情</DialogTitle>
          </DialogHeader>
          {detail && (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-1.5">
                {detail.tags.map((t) => (
                  <Badge key={t} variant="outline">
                    {t}
                  </Badge>
                ))}
              </div>
              <MathText
                text={splitStem(detail.content_markdown).stem}
                className="text-sm text-slate-800"
              />
              {splitStem(detail.content_markdown).analysis && (
                <div className="border-t pt-3">
                  <MathText
                    text={splitStem(detail.content_markdown).analysis!}
                    className="text-sm text-slate-600"
                  />
                </div>
              )}
              <div className="flex flex-wrap items-baseline gap-1 text-sm font-semibold text-slate-900">
                <span>答案：</span>
                <MathText text={detail.answer} />
              </div>
              {/* 同类题检测：按 key 随题目切换重置面板状态 */}
              <DetectionPanel key={detail.id} questionId={detail.id} />
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
