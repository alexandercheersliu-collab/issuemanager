/** Web Speech API 语音识别封装：中文、实时临时结果、说完回传最终文本。
 *
 * 浏览器不支持时 supported=false，调用方隐藏语音入口即可；
 * 后续计划加后端 Whisper 识别做全端兜底（README 路线图登记）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

interface RecognitionAlternative {
  transcript: string
}
interface RecognitionResult {
  isFinal: boolean
  0: RecognitionAlternative
}
interface RecognitionEvent {
  resultIndex: number
  results: { length: number; [i: number]: RecognitionResult }
}
interface Recognition {
  lang: string
  interimResults: boolean
  continuous: boolean
  onresult: ((e: RecognitionEvent) => void) | null
  onend: (() => void) | null
  onerror: (() => void) | null
  start(): void
  stop(): void
}
type RecognitionCtor = new () => Recognition

function getCtor(): RecognitionCtor | null {
  const w = window as unknown as {
    SpeechRecognition?: RecognitionCtor
    webkitSpeechRecognition?: RecognitionCtor
  }
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null
}

export function useSpeechRecognition(onFinal: (text: string) => void) {
  const [supported] = useState(() => getCtor() !== null)
  const [listening, setListening] = useState(false)
  const [interim, setInterim] = useState('')
  const recRef = useRef<Recognition | null>(null)
  const onFinalRef = useRef(onFinal)
  useEffect(() => {
    onFinalRef.current = onFinal
  }, [onFinal])

  const stop = useCallback(() => {
    recRef.current?.stop()
  }, [])

  const start = useCallback(() => {
    const Ctor = getCtor()
    if (!Ctor || recRef.current) return
    const rec = new Ctor()
    rec.lang = 'zh-CN'
    rec.interimResults = true
    rec.continuous = false
    rec.onresult = (e) => {
      let finalText = ''
      let interimText = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const result = e.results[i]
        if (result.isFinal) finalText += result[0].transcript
        else interimText += result[0].transcript
      }
      setInterim(interimText)
      if (finalText) {
        onFinalRef.current(finalText)
        setInterim('')
      }
    }
    const cleanup = () => {
      recRef.current = null
      setListening(false)
      setInterim('')
    }
    rec.onend = cleanup
    rec.onerror = cleanup
    recRef.current = rec
    setListening(true)
    rec.start()
  }, [])

  useEffect(() => () => recRef.current?.stop(), [])

  return { supported, listening, interim, start, stop }
}
