/** AI 助手页：语音/文字驱动的错题本 Agent 对话（对接 /agent/chat/stream）。
 *
 * 语音输入走浏览器 Web Speech API，识别结果先填入输入框待确认再发送
 * （数学术语易识别错）；浏览器不支持时隐藏麦克风，退化为纯文字对话。
 */
import { useEffect, useRef, useState } from 'react'
import { Loader2, Mic, Send, Square } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { api, ApiError } from '@/lib/api'
import { useSpeechRecognition } from '@/hooks/use-speech-recognition'
import MathText from '@/components/MathText'
import type { ChatMessage } from '@/types'

const SUGGESTIONS = ['本周学习报告', '今天有哪些要复习的', '搜索 判别式']

export default function Assistant() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  const { supported, listening, interim, start, stop } = useSpeechRecognition((text) =>
    setInput((prev) => prev + text),
  )

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, sending])

  async function send(text: string) {
    const message = text.trim()
    if (!message || sending) return
    if (listening) stop()
    // 历史不含本轮：后端会把 message 追加在 history 之后
    const history = messages.slice(-40)
    setMessages([...messages, { role: 'user', content: message }, { role: 'assistant', content: '' }])
    setInput('')
    setSending(true)
    try {
      await api.agentChatStream(message, history, (delta) => {
        setMessages((prev) => {
          const next = [...prev]
          const last = next[next.length - 1]
          next[next.length - 1] = { ...last, content: last.content + delta }
          return next
        })
      })
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '助手暂时不可用，请稍后再试')
      // 一字未回的占位气泡直接撤掉；已流出部分内容则保留
      setMessages((prev) => (prev.at(-1)?.content === '' ? prev.slice(0, -1) : prev))
    } finally {
      setSending(false)
    }
  }

  const displayValue = listening ? input + interim : input

  return (
    <div className="flex min-h-dvh flex-col px-4 pb-40 pt-6">
      <header className="mb-4">
        <h1 className="text-xl font-bold text-slate-900">AI 助手</h1>
        <p className="text-sm text-slate-500">语音或文字提问，助手帮你管错题</p>
      </header>

      {messages.length === 0 && (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 text-center">
          <p className="text-sm text-slate-400">
            {supported ? '点下方麦克风直接说，或输入文字：' : '试试问我：'}
          </p>
          <div className="flex flex-wrap justify-center gap-2">
            {SUGGESTIONS.map((s) => (
              <Button key={s} variant="outline" size="sm" onClick={() => send(s)}>
                {s}
              </Button>
            ))}
          </div>
        </div>
      )}

      <div className="flex-1 space-y-3">
        {messages.map((m, i) =>
          m.role === 'user' ? (
            <div key={i} className="flex justify-end">
              <p className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-blue-600 px-3.5 py-2.5 text-sm text-white">
                {m.content}
              </p>
            </div>
          ) : (
            <div key={i} className="flex justify-start">
              <div className="max-w-[85%] rounded-2xl rounded-bl-sm bg-white px-3.5 py-2.5 text-sm text-slate-800 shadow-sm">
                {m.content ? (
                  <MathText text={m.content} />
                ) : (
                  sending && i === messages.length - 1 && (
                    <Loader2 className="h-4 w-4 animate-spin text-slate-400" />
                  )
                )}
              </div>
            </div>
          ),
        )}
        <div ref={bottomRef} />
      </div>

      {/* 输入栏：固定在底部导航之上 */}
      <div className="fixed inset-x-0 bottom-[calc(3.5rem+env(safe-area-inset-bottom))] z-10 mx-auto max-w-md border-t border-slate-200 bg-white px-3 py-2">
        <div className="flex items-end gap-2">
          <Textarea
            rows={1}
            className="max-h-32 min-h-10 flex-1 resize-none"
            placeholder={listening ? '正在聆听…' : '输入问题，或点麦克风语音输入'}
            value={displayValue}
            readOnly={listening}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                send(input)
              }
            }}
          />
          {supported && (
            <Button
              type="button"
              size="icon"
              variant={listening ? 'destructive' : 'outline'}
              className="shrink-0"
              onClick={() => (listening ? stop() : start())}
            >
              {listening ? <Square className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
            </Button>
          )}
          <Button
            type="button"
            size="icon"
            className="shrink-0"
            disabled={!input.trim() || sending || listening}
            onClick={() => send(input)}
          >
            {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          </Button>
        </div>
        {listening && <p className="mt-1 text-center text-xs text-red-500">正在识别，说完点 ■ 结束</p>}
      </div>
    </div>
  )
}
